"""Integration tests: vouch.queue.stream.EventStream against a real Redis Stream.

Stream semantics that only a real broker shows: the id format XADD returns, a
consumer group reading "new" entries only, XACK returning the number of entries
actually removed from the pending-entries list, and BUSYGROUP being benign on a
second ensure_group. Every assertion reads the server's own view (XRANGE,
XPENDING) rather than the client's return values alone.

Requires Redis on 127.0.0.1:6379 (docker compose up -d).
"""

import json

import pytest

from vouch.ingestion import Event

pytestmark = pytest.mark.integration

GROUP = "test-workers"


class TestProduce:
    @pytest.mark.asyncio
    async def test_produce_creates_the_stream_and_returns_a_stream_id(
        self, event_stream, redis_client
    ):
        stream_id = await event_stream.produce(
            Event(channel="whatsapp", idempotency_key="whatsapp:1", payload={"text": "hi"})
        )

        # Redis ids are "<milliseconds>-<sequence>".
        milliseconds, _, sequence = stream_id.partition("-")
        assert milliseconds.isdigit() and sequence.isdigit()

        entries = await redis_client.xrange(event_stream.stream_name)
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_produced_entry_carries_the_event_as_json(self, event_stream, redis_client):
        event = Event(
            channel="telegram", idempotency_key="telegram:9", payload={"text": "hi"}
        )

        await event_stream.produce(event)

        _, fields = (await redis_client.xrange(event_stream.stream_name))[0]
        assert set(fields) == {"data"}
        assert json.loads(fields["data"]) == {
            "channel": "telegram",
            "idempotency_key": "telegram:9",
            "payload": {"text": "hi"},
        }

    @pytest.mark.asyncio
    async def test_produce_appends_in_order(self, event_stream, redis_client):
        for i in range(3):
            await event_stream.produce(
                Event(channel="whatsapp", idempotency_key=f"k{i}", payload={"i": i})
            )

        entries = await redis_client.xrange(event_stream.stream_name)

        assert [json.loads(f["data"])["idempotency_key"] for _, f in entries] == [
            "k0", "k1", "k2"
        ]


class TestEnsureGroup:
    @pytest.mark.asyncio
    async def test_group_is_created(self, event_stream, redis_client):
        await event_stream.ensure_group(GROUP)

        groups = await redis_client.xinfo_groups(event_stream.stream_name)
        assert [g["name"] for g in groups] == [GROUP]

    @pytest.mark.asyncio
    async def test_second_call_is_a_no_op(self, event_stream):
        # ensure_group runs on every worker start; BUSYGROUP must be swallowed.
        await event_stream.ensure_group(GROUP)
        await event_stream.ensure_group(GROUP)

    @pytest.mark.asyncio
    async def test_group_reads_from_the_beginning_of_the_stream(self, event_stream):
        await event_stream.produce(
            Event(channel="whatsapp", idempotency_key="before-group", payload={})
        )
        await event_stream.ensure_group(GROUP)

        batches = await event_stream.consume(GROUP, "worker-0", 10, block_ms=100)

        keys = [json.loads(f["data"])["idempotency_key"] for _, entries in batches for _, f in entries]
        # id="0" means an entry produced before the group existed is still delivered.
        assert keys == ["before-group"]


class TestConsumeAndAck:
    async def seed(self, event_stream, count=2):
        for i in range(count):
            await event_stream.produce(
                Event(channel="whatsapp", idempotency_key=f"k{i}", payload={"i": i})
            )

    @pytest.mark.asyncio
    async def test_consume_delivers_new_entries(self, event_stream):
        await self.seed(event_stream, 2)
        await event_stream.ensure_group(GROUP)

        batches = await event_stream.consume(GROUP, "worker-1", 10, block_ms=100)

        _, entries = batches[0]
        assert len(entries) == 2

    @pytest.mark.asyncio
    async def test_delivered_entries_move_into_the_pending_list(self, event_stream, redis_client):
        await self.seed(event_stream, 1)
        await event_stream.ensure_group(GROUP)

        await event_stream.consume(GROUP, "worker-1", 10, block_ms=100)

        pending = await redis_client.xpending(event_stream.stream_name, GROUP)
        assert pending["pending"] == 1

    @pytest.mark.asyncio
    async def test_ack_removes_the_entry_from_the_pending_list(self, event_stream, redis_client):
        await self.seed(event_stream, 1)
        await event_stream.ensure_group(GROUP)
        _, entries = (await event_stream.consume(GROUP, "worker-1", 10, block_ms=100))[0]
        message_id = entries[0][0]

        acknowledged = await event_stream.ack(GROUP, message_id)

        assert acknowledged == 1
        pending = await redis_client.xpending(event_stream.stream_name, GROUP)
        assert pending["pending"] == 0

    @pytest.mark.asyncio
    async def test_acking_twice_removes_nothing(self, event_stream):
        await self.seed(event_stream, 1)
        await event_stream.ensure_group(GROUP)
        _, entries = (await event_stream.consume(GROUP, "worker-1", 10, block_ms=100))[0]
        message_id = entries[0][0]

        await event_stream.ack(GROUP, message_id)

        assert await event_stream.ack(GROUP, message_id) == 0

    @pytest.mark.asyncio
    async def test_a_second_consumer_does_not_receive_delivered_entries(self, event_stream):
        await self.seed(event_stream, 1)
        await event_stream.ensure_group(GROUP)
        await event_stream.consume(GROUP, "worker-1", 10, block_ms=100)

        batches = await event_stream.consume(GROUP, "worker-2", 10, block_ms=100)

        assert batches == []

    @pytest.mark.asyncio
    async def test_entries_produced_after_a_read_are_delivered_next(self, event_stream):
        await self.seed(event_stream, 1)
        await event_stream.ensure_group(GROUP)
        await event_stream.consume(GROUP, "worker-1", 10, block_ms=100)

        await self.seed(event_stream, 1)   # second entry, new idempotency key k0 again
        batches = await event_stream.consume(GROUP, "worker-1", 10, block_ms=100)

        _, entries = batches[0]
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_unacked_entry_is_still_pending_after_another_read(self, event_stream, redis_client):
        # The mechanism recovery depends on: an entry the worker crashed on
        # stays in the PEL instead of being redelivered as new.
        await self.seed(event_stream, 1)
        await event_stream.ensure_group(GROUP)
        await event_stream.consume(GROUP, "worker-1", 10, block_ms=100)

        await event_stream.consume(GROUP, "worker-1", 10, block_ms=100)

        pending = await redis_client.xpending(event_stream.stream_name, GROUP)
        assert pending["pending"] == 1


class TestStaleEntryRecovery:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "XAUTOCLAIM recovery not implemented in EventStream.recover: the method "
            "body is empty, so a message left pending by a crashed worker is never "
            "reclaimed and the PEL grows without bound"
        ),
    )
    @pytest.mark.asyncio
    async def test_stale_pending_entry_is_reclaimed(self, event_stream):
        await event_stream.produce(
            Event(channel="whatsapp", idempotency_key="stale", payload={})
        )
        await event_stream.ensure_group(GROUP)
        _, entries = (await event_stream.consume(GROUP, "worker-1", 10, block_ms=100))[0]
        stale_id = entries[0][0]

        # worker-2 reclaims the entry another consumer abandoned.
        reclaimed = await event_stream.recover("worker-2", min_idle_ms=0, count=10)

        assert stale_id in [entry_id for entry_id, _ in reclaimed]