"""Integration tests: vouch.worker against a real Redis Stream.

The worker's contract is a distributed one — claim an idempotency key with
SET NX EX, hand the payload to the handler, acknowledge with XACK — so every
assertion here reads Redis's own state: the lock key, its TTL, and the pending
entries list. A mocked Redis client would prove only that the calls were made,
not that the semantics hold.

Requires Redis on 127.0.0.1:6379 (docker compose up -d).
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from vouch.ingestion import Event
from vouch.worker import GROUP, Worker, WorkerPool

pytestmark = pytest.mark.integration

LOCK_TTL_SECONDS = 3600


class RecordingHandler:
    def __init__(self, on_payload=None):
        self.payloads = []
        self._on_payload = on_payload

    async def handle(self, payload):
        self.payloads.append(payload)
        if self._on_payload:
            self._on_payload(payload)


async def deliver(event_stream, key: str, payload=None, consumer="worker-0"):
    """Produce an event and read it back as this consumer would.

    The entry must come from a real XREADGROUP before the worker touches it:
    that read is what places the message in the pending-entries list, so every
    XACK assertion below is a real acknowledgement rather than a no-op against
    a message the group never saw.
    """
    await event_stream.ensure_group(GROUP)
    await event_stream.produce(
        Event(channel="whatsapp", idempotency_key=key, payload=payload or {"text": key})
    )
    batches = await event_stream.consume(GROUP, consumer, 10, block_ms=100)
    _, entries = batches[0]
    return entries[0]


async def pending_count(event_stream) -> int:
    pending = await event_stream.client.xpending(event_stream.stream_name, GROUP)
    return pending["pending"]


class TestClaimHandleAck:
    @pytest.mark.asyncio
    async def test_successful_run_claims_handles_and_acks(self, event_stream, redis_client):
        entry = await deliver(event_stream, "whatsapp:wamid.1", {"text": "verify now"})
        handler = RecordingHandler()
        assert await pending_count(event_stream) == 1

        await Worker(event_stream, handler, "worker-0").process(entry)

        assert handler.payloads == [{"text": "verify now"}]
        assert await pending_count(event_stream) == 0

    @pytest.mark.asyncio
    async def test_claim_is_a_lock_with_the_declared_ttl(self, event_stream, redis_client):
        entry = await deliver(event_stream, "whatsapp:wamid.1")

        await Worker(event_stream, RecordingHandler(), "worker-0").process(entry)

        # The lock survives a successful run: it is what makes a *redelivery*
        # (crash before XACK) idempotent while the work is still in flight.
        assert await redis_client.get("whatsapp:wamid.1") == "processing"
        ttl = await redis_client.ttl("whatsapp:wamid.1")
        assert 0 < ttl <= LOCK_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_lock_key_is_the_idempotency_key(self, event_stream, redis_client):
        entry = await deliver(event_stream, "telegram:987654321")

        await Worker(event_stream, RecordingHandler(), "worker-0").process(entry)

        assert await redis_client.exists("telegram:987654321") == 1


class TestDuplicateDelivery:
    @pytest.mark.asyncio
    async def test_event_already_claimed_is_acked_without_rehandling(
        self, event_stream, redis_client
    ):
        entry = await deliver(event_stream, "whatsapp:wamid.1")
        await redis_client.set("whatsapp:wamid.1", "processing", ex=LOCK_TTL_SECONDS)
        handler = RecordingHandler()

        await Worker(event_stream, handler, "worker-0").process(entry)

        assert handler.payloads == []
        assert await pending_count(event_stream) == 0

    @pytest.mark.asyncio
    async def test_another_worker_s_claim_is_left_intact(self, event_stream, redis_client):
        entry = await deliver(event_stream, "whatsapp:wamid.1")
        await redis_client.set("whatsapp:wamid.1", "processing", ex=LOCK_TTL_SECONDS)

        await Worker(event_stream, RecordingHandler(), "worker-0").process(entry)

        # Releasing someone else's lock would let the same work run twice.
        assert await redis_client.get("whatsapp:wamid.1") == "processing"

    @pytest.mark.asyncio
    async def test_two_deliveries_of_one_event_run_the_handler_once(
        self, event_stream, redis_client
    ):
        entry = await deliver(event_stream, "whatsapp:wamid.1")
        worker = Worker(event_stream, RecordingHandler(), "worker-0")

        await worker.process(entry)
        await worker.process(entry)   # same delivery replayed

        assert len(worker.handler.payloads) == 1


class TestFailurePath:
    @pytest.mark.asyncio
    async def test_failed_run_releases_its_claim(self, event_stream, redis_client):
        entry = await deliver(event_stream, "whatsapp:wamid.1")
        handler = RecordingHandler(on_payload=lambda payload: 1 / 0)

        with pytest.raises(ZeroDivisionError):
            await Worker(event_stream, handler, "worker-0").process(entry)

        assert await redis_client.exists("whatsapp:wamid.1") == 0

    @pytest.mark.asyncio
    async def test_failed_run_is_not_acked_so_it_stays_recoverable(
        self, event_stream, redis_client
    ):
        entry = await deliver(event_stream, "whatsapp:wamid.1")
        handler = RecordingHandler(on_payload=lambda payload: 1 / 0)

        with pytest.raises(ZeroDivisionError):
            await Worker(event_stream, handler, "worker-0").process(entry)

        # Still pending: XAUTOCLAIM recovery (not yet implemented) depends on it.
        assert await pending_count(event_stream) == 1

    @pytest.mark.asyncio
    async def test_a_failed_run_can_be_retried_after_release(self, event_stream):
        entry = await deliver(event_stream, "whatsapp:wamid.1")
        failing = RecordingHandler(on_payload=lambda payload: 1 / 0)

        with pytest.raises(ZeroDivisionError):
            await Worker(event_stream, failing, "worker-0").process(entry)

        succeeding = RecordingHandler()
        await Worker(event_stream, succeeding, "worker-0").process(entry)

        assert len(succeeding.payloads) == 1
        assert await pending_count(event_stream) == 0


class TestConcurrentWorkers:
    @pytest.mark.asyncio
    async def test_two_workers_never_process_the_same_entry(self, event_stream):
        total = 4
        for i in range(total):
            await event_stream.produce(
                Event(channel="whatsapp", idempotency_key=f"whatsapp:{i}",
                      payload={"i": i})
            )
        await event_stream.ensure_group(GROUP)

        processed = {"count": 0}
        done = asyncio.Event()

        def handler_for():
            handler = RecordingHandler(
                on_payload=lambda payload: (
                    processed.__setitem__("count", processed["count"] + 1),
                    done.set() if processed["count"] == total else None,
                )
            )
            return handler

        workers = [
            Worker(event_stream, handler_for(), "worker-0"),
            Worker(event_stream, handler_for(), "worker-1"),
        ]
        tasks = [asyncio.create_task(w.run()) for w in workers]
        try:
            await asyncio.wait_for(done.wait(), timeout=10)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        seen = [p["i"] for w in workers for p in w.handler.payloads]
        assert sorted(seen) == list(range(total))
        assert len(seen) == len(set(seen))

    @pytest.mark.asyncio
    async def test_pool_of_workers_drains_the_stream(self, event_stream):
        for i in range(2):
            await event_stream.produce(
                Event(channel="whatsapp", idempotency_key=f"whatsapp:{i}", payload={"i": i})
            )
        await event_stream.ensure_group(GROUP)
        handler = RecordingHandler()
        pool = WorkerPool(event_stream, handler, 2)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(pool.run(), timeout=2)

        assert sorted(p["i"] for p in handler.payloads) == [0, 1]
        assert await pending_count(event_stream) == 0