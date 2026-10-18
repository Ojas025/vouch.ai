"""
Reusable Redis Stream service with connection pooling
"""

from typing import Any
from dataclasses import fields, is_dataclass
import json

from redis import ResponseError
from redis.asyncio import Redis, ConnectionPool

from vouch.ingestion import Event
from vouch.observability.propagation import inject_trace_context


def _jsonable(payload: Any) -> Any:
    """Shallow dataclass->dict conversion. Shallow on purpose: asdict deep-copies
    every leaf, and payload holds the raw provider webhook body."""
    if is_dataclass(payload) and not isinstance(payload, type):
        return {f.name: getattr(payload, f.name) for f in fields(payload)}
    return payload


class EventStream:
    def __init__(self, redis_url: str, stream_name: str):
        self.stream_name = stream_name

        self.pool = ConnectionPool.from_url(url=redis_url, max_connections=20, decode_responses=True)
        self.client = Redis(connection_pool=self.pool)

    async def produce(self, payload: Event, trace_context: dict | None = None) -> bytes | str:
        """Producer method: appends a task to the event stream.

        ``trace_context`` is the W3C carrier of the request that originated the
        event. It rides inside the ``data`` body rather than as a second Redis
        field, so the message contract is unchanged; when it is None the body is
        byte-identical to what this produced before.
        """
        body = _jsonable(payload)
        if trace_context:
            body = inject_trace_context(dict(body), trace_context)
        return await self.client.xadd(
                self.stream_name,
                {"data": json.dumps(body)},
                maxlen=100_000,
                approximate=True
        )

    async def ensure_group(self, group_name: str):
        """
        Creates consumer group associated with the stream if not present.
        When a consumer group with the same name already exists, the command returns a -BUSYGROUP error
        """
        try:
            await self.client.xgroup_create(self.stream_name, group_name, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                # Raise only if it errors something apart from group already exists
                raise

    async def delete_group(self, group_name: str):
        """Deletes the existing consumer group."""
        await self.client.xgroup_destroy(self.stream_name, group_name)

    async def consume(self, group_name: str, consumer_id: str, count: int, block_ms: int = 500):
        """
        Read new entries to the queue for this consumer.
        > : deliver only messages not yet received by any consumer in the group.
        """
        return await self.client.xreadgroup(
            groupname=group_name,
            consumername=consumer_id,
            streams={self.stream_name: ">"},
            count=count,
            block=block_ms
        )

    async def recover(self, consumer_id: str, min_idle_ms: int, count: int = 10):
        """"""

    async def ack(self, group_name: str, message_id: str):
        """Acknowledge and mark the message as processed, removing it from the pending entries list (PEL)"""
        return await self.client.xack(self.stream_name, group_name, message_id)

    async def close(self):
        """Gracefully close all open TCP connections."""
        await self.pool.disconnect()
        await self.client.aclose()
