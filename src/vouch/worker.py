import asyncio
import time
import json
import os

from redis import RedisError
from vouch import pipeline
from vouch.idempotency import IdempotencyStatus
from vouch.logger import configure_logging, get_logger
from vouch.observability.context import bind_correlation
from vouch.observability.propagation import extract_trace_context
from vouch.observability.spans import ATTR_JOB_IDEMPOTENCY_KEY, SPAN_JOB_PROCESS, span
from vouch.queue.stream import EventStream

"""
Idempotency - A worker can process -> crash before XACK
"""

STREAM = "vouch:jobs"
GROUP = "vouch-workers"
CONSUMER = os.getenv("HOSTNAME", "worker-1")

PROCESSING_TTL = 300
COMPLETED_TTL = 86400 * 7

RECOVERY_INTERVAL = 30

logger = get_logger(__name__)

class Worker:
    def __init__(self, stream: EventStream, consumer_id: str):
        # The handler is injected rather than imported: it is the seam where
        # the pipeline runner plugs in, and it keeps this module importable
        # without dragging the whole pipeline/router chain along.
        self.stream = stream
        self.consumer_id = consumer_id
        self.last_recovery = time.monotonic()

    async def run(self) -> None:
        while True:
            try:
                results = await self.stream.consume(GROUP, self.consumer_id, 10)

                # XREADGROUP replies with a (stream_name, entries) pair per stream.
                for _, messages in results:
                    await self.handle_messages(messages)

                now = time.monotonic()

                if now - self.last_recovery >= RECOVERY_INTERVAL:
                    async for messages in self.stream.recover(GROUP, self.consumer_id, min_idle_ms=60_000, count=10):
                        await self.handle_messages(messages)

                    self.last_recovery = now

            except RedisError as r:
                logger.error(f"Redis error: {str(r)}")
                await asyncio.sleep(2)

    async def handle_messages(self, messages):
        for message_id, fields in messages:
            try:
                event = json.loads(fields["data"])
                idempotency_key = event["idempotency_key"]

                await self.process_job(message_id, event, idempotency_key)

            except Exception as e:
                logger.error(
                    "job handler failed",
                    extra={
                        "event": "job.failed",
                        "component": "worker",
                        "consumer_id": self.consumer_id,
                        "error_category": type(e).__name__,
                        "outcome": "failure",
                    },
                    exc_info=True,
                )
                continue

            await self.stream.ack(GROUP, message_id)

    async def process_job(self, message_id, event, idempotency_key) -> None:
        parent = extract_trace_context(event)

        claimed = await self.stream.client.set(idempotency_key, IdempotencyStatus.PROCESSING, nx=True, ex=PROCESSING_TTL)

        if not claimed:
            # Duplicate event
            logger.debug(
                "duplicate event skipped",
                extra={
                    "event": "job.duplicate_skipped",
                    "component": "worker",
                    "idempotency_key": idempotency_key,
                    "consumer_id": self.consumer_id,
                },
            )
            return

        logger.info(
            "job claimed",
            extra={
                "event": "job.started",
                "component": "worker",
                "idempotency_key": idempotency_key,
                "consumer_id": self.consumer_id,
            },
        )

        try:
            with span(
                SPAN_JOB_PROCESS,
                {ATTR_JOB_IDEMPOTENCY_KEY: idempotency_key},
                context=parent,
            ):
                with bind_correlation(job_id=idempotency_key):
                    await pipeline.run(event)
            logger.info(
                "job handled and acknowledged",
                extra={
                    "event": "job.completed",
                    "component": "worker",
                    "idempotency_key": idempotency_key,
                    "consumer_id": self.consumer_id,
                    "outcome": "ok",
                },
            )
        except Exception as e:
            logger.error(
                "job handler failed",
                extra={
                    "event": "job.failed",
                    "component": "worker",
                    "idempotency_key": idempotency_key,
                    "consumer_id": self.consumer_id,
                    "error_category": type(e).__name__,
                    "outcome": "failure",
                },
                exc_info=True,
            )
            await self.stream.client.delete(idempotency_key)
            raise

class WorkerPool:
    def __init__(self, stream: EventStream, total_workers: int):
        self.stream = stream
        self.total_workers = total_workers

    async def run(self):
        workers = [
            Worker(
                consumer_id=f"{CONSUMER}-worker-{i}",
                stream=self.stream,
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

    try:
        await stream.ensure_group(GROUP)

        await WorkerPool(
            stream=stream,
            total_workers=MAX_WORKERS
        ).run()

    finally:
        await stream.close()


if __name__ == "__main__":
    asyncio.run(main())
