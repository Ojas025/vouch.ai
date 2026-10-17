import asyncio
import json
import os

from vouch.stream import EventStream

"""
Idempotency - A worker can process -> crash before XACK

"""

STREAM = "jobs"
GROUP = "workers"
CONSUMER = os.getenv("HOSTNAME", "worker-1")

class Worker:
    def __init__(self, stream: EventStream, handler, consumer_id: str):
        # The handler is injected rather than imported: it is the seam where
        # the pipeline runner plugs in, and it keeps this module importable
        # without dragging the whole pipeline/router chain along.
        self.stream = stream
        self.handler = handler
        self.consumer_id = consumer_id

    async def run(self) -> None:
        while True:
            batches = await self.stream.consume(GROUP, self.consumer_id, 10)

            # XREADGROUP replies with a (stream_name, entries) pair per stream.
            for _stream_name, entries in batches:
                for entry in entries:
                    await self.process(entry)

    async def process(self, entry) -> None:
        message_id, fields = entry
        event = json.loads(fields["data"])
        idempotency_key = event["idempotency_key"]

        claimed = await self.stream.client.set(
            idempotency_key, "processing", nx=True, ex=3600
        )

        if not claimed:
            # Duplicate event
            await self.stream.ack(GROUP, message_id)
            return

        try:
            await self.handler.handle(event["payload"])
            await self.stream.ack(GROUP, message_id)
        except Exception:
            await self.stream.client.delete(idempotency_key)
            raise

class WorkerPool:
    def __init__(self, stream: EventStream, handler, total_workers: int):
        self.stream = stream
        self.handler = handler
        self.total_workers = total_workers

    async def run(self):
        workers = [
            Worker(
                consumer_id=f"worker-{i}",
                stream=self.stream,
                handler=self.handler,
            )
            for i in range(self.total_workers)
        ]

        await asyncio.gather(
            *(worker.run() for worker in workers)
        )

# --- Crash recovery (moved from worker/recovery.py) ---

async def recover():
   pass

# Must match the stream the API producer writes to in vouch.api.server.
EVENTS_STREAM = "events:stream"
MAX_WORKERS = int(os.getenv("VOUCH_WORKER_COUNT", "4"))


async def main() -> None:
    """Entrypoint for the consumer pool. The group must exist before XREADGROUP."""
    stream = EventStream(
        redis_url=os.environ["REDIS_URL"],
        stream_name=EVENTS_STREAM,
    )

    await stream.ensure_group(GROUP)

    # TODO: inject the pipeline handler; WorkerPool requires one at construction.
    await WorkerPool(stream=stream, handler=None, total_workers=MAX_WORKERS).run()


if __name__ == "__main__":
    asyncio.run(main())
