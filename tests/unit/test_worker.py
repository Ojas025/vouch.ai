"""Unit tests for vouch.worker.

Worker.process() and Worker.run() are async, so every test is marked. The
Redis client, EventStream and EventHandler are all replaced: no broker, no
handler implementation and no database is involved.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import vouch.worker as worker_module
from vouch.worker import GROUP, Worker, WorkerPool


class TestModuleImports:
    def test_worker_and_pipeline_import_chain_is_clean(self):
        # The collection of this file once died on the worker -> pipeline ->
        # router chain (NameError: RoutePlan). Importing both here is the
        # regression guard for that chain.
        import vouch.pipeline

        assert vouch.worker.Worker is Worker
        assert callable(vouch.pipeline.run)


@pytest.fixture
def stream():
    s = AsyncMock()
    s.client = AsyncMock()
    s.client.set = AsyncMock(return_value=True)
    s.client.delete = AsyncMock()
    s.ack = AsyncMock()
    s.consume = AsyncMock(return_value=[])
    return s


@pytest.fixture
def handler():
    h = AsyncMock()
    h.handle = AsyncMock()
    return h


@pytest.fixture
def stream_entry():
    """Realistic Redis stream entry tuple yielded by xreadgroup."""
    return (
        "1700000000000-0",
        {
            "data": json.dumps({
                "idempotency_key": "whatsapp:wamid.1",
                "payload": {"text": "hi"},
            })
        },
    )


class TestWorkerProcess:
    @pytest.mark.asyncio
    async def test_claims_handles_and_acks_against_the_real_redis_module(
        self, stream, handler, stream_entry
    ):
        # No patching of the redis module: worker.py must work with the client
        # it actually imports, not a substitute that happens to have .set().
        await Worker(stream, handler, "worker-0").process(stream_entry)

        handler.handle.assert_awaited_once_with({"text": "hi"})
        stream.ack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_claims_then_handles_then_acks_using_stream_client(
        self, stream, handler, stream_entry
    ):
        await Worker(stream, handler, "worker-0").process(stream_entry)

        stream.client.set.assert_awaited_once_with(
            "whatsapp:wamid.1", "processing", nx=True, ex=3600
        )
        handler.handle.assert_awaited_once_with({"text": "hi"})
        stream.ack.assert_awaited_once_with(GROUP, "1700000000000-0")

    @pytest.mark.asyncio
    async def test_processes_an_entry_as_returned_by_consume(self, stream, handler):
        # EventStream.consume() yields raw XREADGROUP tuples. process() must
        # handle that shape rather than assuming attribute access.
        entry = ("1700000000000-0", {"data": json.dumps(
            {"idempotency_key": "telegram:1", "payload": {"text": "hi"}}
        )})

        await Worker(stream, handler, "worker-0").process(entry)

        stream.ack.assert_awaited_once_with(GROUP, "1700000000000-0")

    @pytest.mark.asyncio
    async def test_duplicate_event_is_acked_without_handling(
        self, stream, handler, stream_entry
    ):
        stream.client.set.return_value = None  # key already held

        await Worker(stream, handler, "worker-0").process(stream_entry)

        handler.handle.assert_not_awaited()
        stream.ack.assert_awaited_once_with(GROUP, "1700000000000-0")
        stream.client.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failure_deletes_claim_and_reraises(
        self, stream, handler, stream_entry
    ):
        handler.handle.side_effect = RuntimeError("handler exploded")

        with pytest.raises(RuntimeError, match="handler exploded"):
            await Worker(stream, handler, "worker-0").process(stream_entry)

        stream.client.delete.assert_awaited_once_with("whatsapp:wamid.1")
        stream.ack.assert_not_awaited()


class TestWorkerRun:
    @pytest.mark.asyncio
    async def test_consumes_from_the_shared_group(self, handler):
        # consume() must actually suspend, otherwise run()'s while-loop spins
        # faster than wait_for's cancellation can be delivered and the test
        # never yields control back. It must also return an iterable of
        # events, since run() iterates the result directly.
        async def blocking_consume(*args, **kwargs):
            await asyncio.sleep(3600)
            return []

        stream = AsyncMock()
        stream.consume = AsyncMock(side_effect=blocking_consume)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                Worker(stream, handler, "worker-7").run(), timeout=0.05
            )

        stream.consume.assert_awaited_with(GROUP, "worker-7", 10)

    @pytest.mark.asyncio
    async def test_processes_each_returned_event_once(self, handler):
        entries = [
            (f"id-{i}", {"data": json.dumps({"idempotency_key": f"k-{i}", "payload": {}})})
            for i in range(3)
        ]
        redis_stream_response = [("jobs", entries)]

        async def consume_then_block(*args, **kwargs):
            if stream.consume.await_count > 1:
                return await asyncio.sleep(3600)
            return redis_stream_response

        stream = AsyncMock()
        stream.consume = AsyncMock(side_effect=consume_then_block)

        worker = Worker(stream, handler, "worker-0")
        with patch.object(worker, "process", AsyncMock()) as process:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(worker.run(), timeout=0.1)

        assert process.await_count == len(entries)

    @pytest.mark.asyncio
    async def test_polls_continuously_while_stream_is_empty(self, handler):
        async def empty_then_block(*args, **kwargs):
            if stream.consume.await_count >= 3:
                return await asyncio.sleep(3600)
            return []

        stream = AsyncMock()
        stream.consume = AsyncMock(side_effect=empty_then_block)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                Worker(stream, handler, "worker-0").run(), timeout=0.2
            )

        assert stream.consume.await_count >= 3

    @pytest.mark.asyncio
    async def test_handler_failure_stops_the_loop(self, handler):
        # Must deliver a real xreadgroup-shaped response so process() is
        # actually reached; otherwise the assertion could pass vacuously.
        async def deliver_one_event(*args, **kwargs):
            if stream.consume.await_count > 1:
                return await asyncio.sleep(3600)
            return [("jobs", [("id-1", {"data": json.dumps({"idempotency_key": "k-1", "payload": {}})})])]

        stream = AsyncMock()
        stream.consume = AsyncMock(side_effect=deliver_one_event)
        handler.handle.side_effect = RuntimeError("handler exploded")

        with pytest.raises(RuntimeError, match="handler exploded"):
            await asyncio.wait_for(
                Worker(stream, handler, "worker-0").run(), timeout=0.2
            )

        # A failed event must not be acked, and the loop must not poll again.
        stream.ack.assert_not_awaited()
        assert stream.consume.await_count == 1


class TestWorkerPool:
    def test_stores_total_workers(self, stream, handler):
        pool = WorkerPool(stream, handler, 4)
        assert pool.total_workers == 4
        assert pool.stream is stream
        assert pool.handler is handler

    @pytest.mark.asyncio
    async def test_creates_one_worker_per_slot_with_indexed_ids(
        self, stream, handler
    ):
        pool = WorkerPool(stream, handler, 3)

        with patch.object(worker_module, "Worker") as WorkerCls:
            WorkerCls.return_value.run = AsyncMock()
            await asyncio.wait_for(pool.run(), timeout=0.05)

        ids = [c.kwargs["consumer_id"] for c in WorkerCls.call_args_list]
        assert ids == ["worker-0", "worker-1", "worker-2"]

    @pytest.mark.asyncio
    async def test_shares_stream_and_handler_across_workers(self, stream, handler):
        pool = WorkerPool(stream, handler, 2)

        with patch.object(worker_module, "Worker") as WorkerCls:
            WorkerCls.return_value.run = AsyncMock()
            await asyncio.wait_for(pool.run(), timeout=0.05)

        # Assert the call count first: iterating an empty list would otherwise
        # make this pass vacuously.
        assert len(WorkerCls.call_args_list) == 2
        for c in WorkerCls.call_args_list:
            assert c.kwargs["stream"] is stream
            assert c.kwargs["handler"] is handler

    @pytest.mark.asyncio
    async def test_runs_workers_concurrently(self, stream, handler):
        # A barrier, not sleep(0): each worker blocks until BOTH have started,
        # so this only completes if the pool actually runs them concurrently.
        # Sequential execution would deadlock and time out.
        pool = WorkerPool(stream, handler, 2)
        started = asyncio.Event()
        arrivals = []

        class FakeWorker:
            def __init__(self, **kw):
                self.consumer_id = kw["consumer_id"]

            async def run(self):
                arrivals.append(self.consumer_id)
                if len(arrivals) == 2:
                    started.set()
                await asyncio.wait_for(started.wait(), timeout=0.5)

        with patch.object(worker_module, "Worker", FakeWorker):
            await asyncio.wait_for(pool.run(), timeout=1.0)

        assert sorted(arrivals) == ["worker-0", "worker-1"]

    @pytest.mark.asyncio
    async def test_zero_workers_completes_immediately(self, stream, handler):
        await asyncio.wait_for(WorkerPool(stream, handler, 0).run(), timeout=0.5)
