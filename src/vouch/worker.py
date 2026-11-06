import asyncio
import time
import json
import os
from typing import Any, Literal

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ValidationError
from redis import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from vouch import pipeline
from vouch.db.models import Investigations, Submission
from vouch.db.session import SessionLocal
from vouch.investigation import complete_investigation, handle_failure
from vouch.lease import LEASE_TTL, LeaseKeeper, LeaseManager
from vouch.logger import configure_logging, get_logger
from vouch.observability.context import bind_correlation
from vouch.observability.propagation import extract_trace_context
from vouch.observability.spans import SPAN_JOB_PROCESS, span
from vouch.queue.stream import EventStream
from vouch.utils import uuid7

"""
Idempotency - A worker can process -> crash before XACK
"""

STREAM = "vouch:jobs"
GROUP = "vouch-workers"

RECOVERY_INTERVAL = 30
RENEW_INTERVAL = LEASE_TTL / 3

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

class DuplicateRequest(Exception):
    """Duplicate request encountered"""
    ...

class LeaseAcquisitionFailed(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

class Worker:
    def __init__(self, stream: EventStream, leases: LeaseManager, worker_id: UUID):
        # The handler is injected rather than imported: it is the seam where
        # the pipeline runner plugs in, and it keeps this module importable
        # without dragging the whole pipeline/router chain along.
        self.stream = stream
        self.worker_id = worker_id

        self.leases = leases

        self.last_recovery = time.monotonic()

    async def run(self) -> None:
        while True:
            try:
                results = await self.stream.consume(GROUP, str(self.worker_id), 10)

                # XREADGROUP replies with a (stream_name, entries) pair per stream.
                for _, messages in results:
                    await self.handle_messages(messages)

                now = time.monotonic()

                if now - self.last_recovery >= RECOVERY_INTERVAL:
                    async for messages in self.stream.recover(GROUP, str(self.worker_id), min_idle_ms=60_000, count=10):
                        await self.handle_messages(messages)

                    self.last_recovery = now

            except RedisError as r:
                logger.error(f"Redis error: {str(r)}")
                await asyncio.sleep(2)

    async def handle_messages(self, messages):
        for message_id, fields in messages:
            try:
                event = json.loads(fields["data"])

                await self.process_job(message_id, event)

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
                continue

            except DuplicateRequest as d:
                logger.error(
                    "duplicate investigation request",
                    extra={
                        "event": "job.duplicate",
                        "component": "worker",
                        "worker_id": self.worker_id,
                        "error_category": type(d).__name__,
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

            await self.stream.ack(GROUP, message_id)

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
                    investigation, is_duplicate = await get_or_create_investigation(SessionLocal, event["submission_id"])

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
                    lease = await self.leases.acquire(investigation.investigation_id, self.worker_id)
                    if lease is None:
                        raise LeaseAcquisitionFailed(
                            f"Lease could not be acquired for worker - {self.worker_id}"
                        )

                    keeper = LeaseKeeper(self.leases, lease)
                    keeper.start()

                    processing = asyncio.create_task(pipeline.run(
                        channel=event["channel"],
                        submission_id=investigation.submission_id,
                        investigation_id=investigation.investigation_id,
                        payload=investigation.payload,
                    ))

                    wait_for_loss = asyncio.create_task(keeper.wait_for_loss())

                    done, pending = await asyncio.wait(
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

                        await keeper.stop()

                        raise LeaseAcquisitionFailed(
                            f"Lease lost during processing for worker - {self.worker_id}"
                        )

                    result = None
                    try:
                        result = await processing

                    except Exception as e:
                        logger.exception(f"Pipeline execution failed: {str(e)}")
                        await handle_failure(str(e), keeper.lease)

                    finally:
                        wait_for_loss.cancel()

                        await wait_for_loss
                        await keeper.stop()

                    if result is not None:
                        await complete_investigation(result, keeper.lease)

            logger.info(
                "job handled and acknowledged",
                extra={
                    "event": "job.completed",
                    "component": "worker",
                    "worker_id": self.worker_id,
                    "outcome": "ok",
                },
            )

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

        return result

class WorkerPool:
    def __init__(self, stream: EventStream, leases: LeaseManager, total_workers: int):
        self.stream = stream
        self.total_workers = total_workers
        self.leases = leases

    async def run(self):
        workers = [
            Worker(
                worker_id=uuid7(),
                stream=self.stream,
                leases=self.leases
            )
            for i in range(self.total_workers)
        ]

        await asyncio.gather(
            *(worker.run() for worker in workers)
        )

EVENTS_STREAM = "events:stream"
MAX_WORKERS = int(os.getenv("VOUCH_WORKER_COUNT", "4"))

async def main() -> None:
    """Entrypoint for the consumer pool. The group must exist before XREADGROUP."""
    configure_logging()

    stream = EventStream(
        redis_url=os.environ["REDIS_URL"],
        stream_name=EVENTS_STREAM,
    )

    leases = LeaseManager(SessionLocal())

    try:
        await stream.ensure_group(GROUP)

        await WorkerPool(
            stream=stream,
            total_workers=MAX_WORKERS,
            leases=leases
        ).run()

    finally:
        await stream.close()


if __name__ == "__main__":
    asyncio.run(main())
