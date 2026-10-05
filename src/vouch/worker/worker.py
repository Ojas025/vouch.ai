import asyncio
import json
import os

import redis.asyncio as redis

from vouch.pipeline import run
from vouch.queue.stream import EventStream

"""
Idempotency - A worker can process -> crash before XACK

"""

STREAM = "jobs"
GROUP = "workers"
CONSUMER = os.getenv("HOSTNAME", "worker-1")

class Worker:
    def __init__(self, stream: EventStream, consumer_id: str):
        self.stream = stream
        self.consumer_id = consumer_id

    async def run(self) -> None:
        while True:
            events = await self.stream.consume(GROUP, self.consumer_id, 10)

            for event in events:
                await self.process(event)

    async def process(self, event) -> None:
        claimed = await redis.set(event.idempotency_key, "processing", nx=True, ex=3600)

        if not claimed:
            # Duplicate event
            await self.stream.ack(GROUP, event.id)
            return

        try:
            await run(event)
            await self.stream.ack(GROUP, event.id)
        except Exception:
            await redis.delete(event.idempotency_key)
            raise

class WorkerPool:
    def __init__(self, stream: EventStream, total_workers: int):
        self.stream = stream
        self.total_workers = total_workers

    async def run(self):
        workers = [
            Worker(
                consumer_id=f"worker-{i}",
                stream=self.stream
            )
            for i in range(self.total_workers)
        ]

        await asyncio.gather(
            *(worker.run() for worker in workers)
        )
