import asyncio
import time
import json
from typing import Any, Literal

from datetime import datetime
from uuid import UUID
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ValidationError
from redis import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from dotenv import load_dotenv

from vouch import pipeline
from vouch.agents.agents import build_graph
from vouch.config import settings
from vouch.context import WorkerContext, init_worker_context
from vouch.db.models import Investigations, Submission
from vouch.investigation import ExecutionOutcome, InvalidLease, complete_investigation, handle_failure
from vouch.lease import LeaseKeeper
from vouch.logger import configure_logging, get_logger
from vouch.observability.context import bind_correlation
from vouch.observability.propagation import extract_trace_context
from vouch.observability.spans import SPAN_JOB_PROCESS, span
from vouch.tools.tools.entity import aclose as close_entity_client
from vouch.utils import uuid7

"""
Idempotency - A worker can process -> crash before XACK
"""
load_dotenv()

logger = get_logger(__name__)

class Investigation(BaseModel):
    investigation_id: UUID
    submission_id: UUID
    payload: str
    sha256_digest: str
    reason: Literal["initial", "manual"] = "initial"
    status: str = "received"
    attempts: int = 0
    max_attempts: int
    not_before: datetime | None = None

    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    lease_renewed_at: datetime | None = None
    fencing_token: int

    result: dict | None = None
    last_error: str | None = None


async def get_or_create_investigation(session_factory: async_sessionmaker[AsyncSession], submission_id: str):
    """Create new Investigation or fetch existing under partial unique index."""
    async with session_factory() as session:
        async with session.begin():
            submission = (
                await session.execute(
                select(Submission.payload, Submission.sha256_digest).where(
                    Submission.id == submission_id
                )
            )
            ).one_or_none()

            if submission is None:
                raise RuntimeError("Persisted submission not found")

            investigation = (
                await session.execute(
                    insert(Investigations).values(
                        submission_id=submission_id,
                        status="received",
                        reason="initial",
                        attempts=0,
                        payload=submission.payload,
                        sha256_digest=submission.sha256_digest
                    ).on_conflict_do_nothing(
                        index_elements=[Submission.id],
                        index_where=Investigations.reason == "initial"
                    ).returning(Investigations)
                )
            ).scalar_one_or_none()

            is_duplicate=False

            if investigation is None:
                # Investigation already exists
                investigation = (
                    await session.execute(
                        select(Investigations).where(
                            Investigations.submission_id == submission_id,
                            Investigations.reason == "initial"
                        )
                    )
                ).scalar_one()

                is_duplicate=True

    return investigation, is_duplicate

class LeaseAcquisitionFailed(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

class Worker:
    def __init__(self, ctx: WorkerContext, graph: CompiledStateGraph, worker_id: UUID):
        self.ctx = ctx
        self.graph = graph
        self.worker_id = worker_id

        self.last_recovery = time.monotonic()

    async def run(self) -> None:
        while True:
            try:
                results = await self.ctx.stream.consume(settings.job_queue_group_name, str(self.worker_id), 10)

                # XREADGROUP replies with a (stream_name, entries) pair per stream.
                for _, messages in results:
                    await self.handle_messages(messages)

                now = time.monotonic()

                if now - self.last_recovery >= settings.recovery_interval:
                    async for messages in self.ctx.stream.recover(settings.job_queue_group_name, str(self.worker_id), min_idle_ms=60_000, count=10):
                        await self.handle_messages(messages)

                    self.last_recovery = now

            except RedisError as r:
                logger.error(f"Redis error: {str(r)}")
                await asyncio.sleep(2)

    async def handle_messages(self, messages):
        for message_id, fields in messages:
            try:
                event = json.loads(fields["data"])

                outcome: ExecutionOutcome = await self.process_job(message_id, event)

                match outcome:

                    case ExecutionOutcome.COMPLETED:
                        # acknowledge completion
                        logger.info(f"Successfully completed job investigation - {message_id}")

                    case ExecutionOutcome.RETRY:
                        # nack -> scheduled for retry -> xautoclaim picks it up
                        logger.warning(f"Failed job investigation, scheduled for retry - {message_id}")
                        await self.ctx.stream.nack(settings.job_queue_group_name, message_id)
                        continue

                    case ExecutionOutcome.DEAD:
                        # dead-letter -> acknowledge
                        logger.warning(f"Exhausted investigation retries, job should drop dead - {message_id}")

                await self.ctx.stream.ack(settings.job_queue_group_name, message_id)

            except ValidationError as e:
                logger.error(
                    "malformed payload from redis stream",
                    extra={
                        "event": "job.malformed_payload",
                        "component": "worker",
                        "worker_id": self.worker_id,
                        "error_category": type(e).__name__,
                    }
                )

                # dead-letter -> ack
                await self.ctx.stream.ack(settings.job_queue_group_name, message_id)

                continue

            except InvalidLease:
                logger.warning(
                    "Investigation lease lost; leaving message pending",
                    extra={
                        "event": "job.lease_lost",
                        "component": "worker",
                        "worker_id": self.worker_id,
                        "message_id": message_id,
                    }
                )
                continue

            except LeaseAcquisitionFailed:
                logger.info(
                    "Lease unavailable; leaving message pending",
                    extra={
                        "event": "job.lease_unavailable",
                        "component": "worker",
                        "worker_id": self.worker_id,
                        "message_id": message_id,
                    }
                )
                continue

            except Exception as e:
                logger.error(
                    "job handler failed",
                    extra={
                        "event": "job.failed",
                        "component": "worker",
                        "worker_id": self.worker_id,
                        "error_category": type(e).__name__,
                        "outcome": "failure",
                    },
                    exc_info=True,
                )
                continue

    async def process_job(self, message_id: str, event) -> Any:
        parent = extract_trace_context(event)

        logger.info(
            "job claimed",
            extra={
                "event": "job.started",
                "component": "worker",
                "worker_id": self.worker_id,
            },
        )

        try:
            with span(
                SPAN_JOB_PROCESS,
                context=parent
            ):
                with bind_correlation():
                    investigation, is_duplicate = await get_or_create_investigation(self.ctx.session_factory, event["submission_id"])

                    # Validate into pydantic model
                    investigation = Investigation(
                        investigation_id=investigation.investigation_id,
                        submission_id=investigation.submission_id,
                        payload=investigation.payload,
                        sha256_digest=investigation.sha256_digest,
                        reason="initial",
                        status="received",
                        attempts=investigation.attempts or 0,
                        max_attempts=investigation.max_attempts or 3,
                        not_before=investigation.not_before,
                        lease_owner=str(investigation.lease_owner) if investigation.lease_owner else None,
                        lease_expires_at=investigation.lease_expires_at,
                        lease_renewed_at=investigation.lease_renewed_at,
                        fencing_token=investigation.fencing_token,
                        result=investigation.result,
                        last_error=investigation.last_error,
                    )

                    # Acquire lease
                    lease = await self.ctx.leases.acquire(investigation.investigation_id, self.worker_id)
                    if lease is None:
                        raise LeaseAcquisitionFailed(
                            f"Lease could not be acquired for worker - {self.worker_id}"
                        )

                    keeper = LeaseKeeper(self.ctx.leases, lease)

                    keeper.start()

                    processing = None
                    wait_for_loss = None

                    try:
                        processing = asyncio.create_task(pipeline.run(
                            graph=self.graph,
                            channel=event["channel"],
                            submission_id=investigation.submission_id,
                            investigation_id=investigation.investigation_id,
                            payload=investigation.payload,
                        ))

                        wait_for_loss = asyncio.create_task(keeper.wait_for_loss())

                        done, _ = await asyncio.wait(
                            [processing, wait_for_loss],
                            return_when=asyncio.FIRST_COMPLETED
                        )

                        if wait_for_loss in done:
                            # lease renewal failed
                            processing.cancel()

                            await asyncio.gather(
                                processing,
                                return_exceptions=True
                            )

                            raise InvalidLease(
                                f"Lease lost during processing for worker - {self.worker_id}"
                            )

                        try:
                            result = await processing

                        except Exception as e:
                            logger.exception("Pipeline execution failed")
                            outcome = await handle_failure(str(e), keeper.lease, 10.0, self.ctx.session_factory)

                        else:
                            outcome = await complete_investigation(result, keeper.lease, self.ctx.session_factory)

                        return outcome

                    finally:
                        if processing and not processing.done():
                            processing.cancel()

                        if wait_for_loss and not wait_for_loss.done():
                            wait_for_loss.cancel()

                        await asyncio.gather(
                            *[processing, wait_for_loss],
                            return_exceptions=True
                        )

                        await keeper.stop()

        except ValidationError as e:
            logger.error(
                "malformed payload from redis stream",
                extra={
                    "event": "job.malformed_payload",
                    "component": "worker",
                    "worker_id": self.worker_id,
                    "error_category": type(e).__name__,
                }
            )
            raise

        except Exception as e:
            logger.error(
                "job handler failed",
                extra={
                    "event": "job.failed",
                    "component": "worker",
                    "worker_id": self.worker_id,
                    "error_category": type(e).__name__,
                    "outcome": "failure",
                },
                exc_info=True,
            )
            raise

class WorkerPool:
    def __init__(self, ctx: WorkerContext, graph: CompiledStateGraph, total_workers: int):
        self.ctx = ctx
        self.graph = graph
        self.total_workers = total_workers

    async def run(self):
        workers = [
            Worker(
                worker_id=uuid7(),
                ctx=self.ctx,
                graph=self.graph
            )
            for i in range(self.total_workers)
        ]

        await asyncio.gather(
            *(worker.run() for worker in workers)
        )

async def main() -> None:
    """Entrypoint for the consumer pool. The group must exist before XREADGROUP."""
    configure_logging()

    ctx = await init_worker_context()
    graph = build_graph(ctx)

    try:
        await ctx.stream.ensure_group(settings.job_queue_group_name)

        await WorkerPool(
            ctx=ctx,
            graph=graph
            total_workers=settings.worker_count,
        ).run()

    finally:
        await close_entity_client()
        await ctx.stream.close()


if __name__ == "__main__":
    asyncio.run(main())
