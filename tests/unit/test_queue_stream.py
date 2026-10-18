"""Unit tests for vouch.stream.EventStream.

The Redis client is an AsyncMock, so no socket is opened and no server is
required. Assertions cover the exact command arguments issued to Redis, which
is the contract other components depend on.
"""

import json
from unittest.mock import AsyncMock, call

import pytest
from redis import ResponseError
from redis.exceptions import ConnectionError as RedisConnectionError

from vouch.ingestion import Event
from vouch.stream import EventStream


@pytest.fixture
def stream():
    s = EventStream(redis_url="redis://localhost:6379/15", stream_name="events:stream")
    s.client = AsyncMock()
    return s


@pytest.fixture
def serializable_payload():
    # EventStream.produce serialises its argument, so use a JSON-native shape.
    return {"idempotency_key": "whatsapp:wamid.1", "payload": {"text": "hi"}}


class TestConstruction:
    def test_stores_stream_name(self, stream):
        assert stream.stream_name == "events:stream"


    def test_pool_is_capped_at_twenty_connections(self):
        s = EventStream(redis_url="redis://localhost:6379/15", stream_name="x")
        assert s.pool.max_connections == 20

    def test_pool_decodes_responses(self):
        # Needed so xreadgroup yields str rather than bytes.
        s = EventStream(redis_url="redis://localhost:6379/15", stream_name="x")
        assert s.pool.connection_kwargs["decode_responses"] is True


class TestProduce:
    @pytest.mark.asyncio
    async def test_appends_to_the_named_stream(self, stream, serializable_payload):
        await stream.produce(serializable_payload)
        args = stream.client.xadd.call_args
        assert args.args[0] == "events:stream"

    @pytest.mark.asyncio
    async def test_wraps_payload_in_data_field_as_json(self, stream, serializable_payload):
        await stream.produce(serializable_payload)
        fields = stream.client.xadd.call_args.args[1]

        assert set(fields) == {"data"}
        assert json.loads(fields["data"]) == serializable_payload

    @pytest.mark.asyncio
    async def test_caps_stream_length(self, stream, serializable_payload):
        await stream.produce(serializable_payload)
        assert stream.client.xadd.call_args.kwargs["maxlen"] == 100_000

    @pytest.mark.asyncio
    async def test_trims_approximately(self, stream, serializable_payload):
        await stream.produce(serializable_payload)
        assert stream.client.xadd.call_args.kwargs["approximate"] is True

    @pytest.mark.asyncio
    async def test_returns_the_redis_stream_id(self, stream, serializable_payload):
        stream.client.xadd.return_value = "1700000000000-0"
        assert await stream.produce(serializable_payload) == "1700000000000-0"


    @pytest.mark.asyncio
    async def test_accepts_the_declared_event_type(self, stream):
        # Event is the declared parameter type of produce(), so it must be
        # serialisable and appended like any other payload.
        await stream.produce(
            Event(channel="telegram", idempotency_key="telegram:1", payload={"a": 1})
        )

        fields = stream.client.xadd.call_args.args[1]
        assert set(fields) == {"data"}
        assert json.loads(fields["data"]) == {
            "channel": "telegram",
            "idempotency_key": "telegram:1",
            "payload": {"a": 1},
        }

    @pytest.mark.asyncio
    async def test_propagates_redis_error(self, stream, serializable_payload):
        stream.client.xadd.side_effect = RedisConnectionError("down")
        with pytest.raises(RedisConnectionError):
            await stream.produce(serializable_payload)


class TestEnsureGroup:
    @pytest.mark.asyncio
    async def test_creates_group_at_id_zero_with_mkstream(self, stream):
        await stream.ensure_group("workers")
        stream.client.xgroup_create.assert_awaited_once_with(
            "events:stream", "workers", id="0", mkstream=True
        )


    @pytest.mark.asyncio
    async def test_reraises_other_response_errors(self, stream):
        stream.client.xgroup_create.side_effect = ResponseError("NOPERM not allowed")
        with pytest.raises(ResponseError, match="NOPERM"):
            await stream.ensure_group("workers")

    @pytest.mark.asyncio
    async def test_swallows_any_error_containing_busygroup_substring(self, stream):
        # Substring match, not an exact check - documented current behaviour.
        stream.client.xgroup_create.side_effect = ResponseError("wrapped BUSYGROUP context")
        await stream.ensure_group("workers")


class TestDeleteGroup:
    @pytest.mark.asyncio
    async def test_destroys_named_group(self, stream):
        await stream.delete_group("workers")
        stream.client.xgroup_destroy.assert_awaited_once_with("events:stream", "workers")

    @pytest.mark.asyncio
    async def test_propagates_missing_group_error(self, stream):
        stream.client.xgroup_destroy.side_effect = ResponseError("NOGROUP no such group")
        with pytest.raises(ResponseError):
            await stream.delete_group("workers")


class TestConsume:
    @pytest.mark.asyncio
    async def test_reads_new_entries_for_group(self, stream):
        await stream.consume("workers", "worker-1", 10)
        kwargs = stream.client.xreadgroup.call_args.kwargs

        assert kwargs["groupname"] == "workers"
        assert kwargs["consumername"] == "worker-1"
        assert kwargs["streams"] == {"events:stream": ">"}



    @pytest.mark.asyncio
    async def test_returns_read_response(self, stream):
        expected = [("events:stream", [("1-0", {"data": "{}"})])]
        stream.client.xreadgroup.return_value = expected
        assert await stream.consume("workers", "worker-1", 10) == expected


class TestRecover:
    @pytest.mark.xfail(reason="EventStream.recover is an empty stub in current implementation")
    @pytest.mark.asyncio
    async def test_recovers_pending_messages_using_autoclaim(self, stream):
        await stream.recover("workers", "worker-1", min_idle_ms=60000, count=10)
        stream.client.xautoclaim.assert_awaited_once()


class TestAck:
    @pytest.mark.asyncio
    async def test_acknowledges_message_in_group(self, stream):
        await stream.ack("workers", "1700000000000-0")
        stream.client.xack.assert_awaited_once_with(
            "events:stream", "workers", "1700000000000-0"
        )

    @pytest.mark.asyncio
    async def test_returns_ack_count(self, stream):
        stream.client.xack.return_value = 1
        assert await stream.ack("workers", "1-0") == 1


class TestClose:
    @pytest.mark.asyncio
    async def test_disconnects_pool_and_closes_client(self, stream):
        stream.pool = AsyncMock()
        await stream.close()

        stream.pool.disconnect.assert_awaited_once()
        stream.client.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnects_pool_before_closing_client(self, stream):
        stream.pool = AsyncMock()
        manager = AsyncMock()
        manager.attach_mock(stream.pool.disconnect, "disconnect")
        manager.attach_mock(stream.client.aclose, "aclose")

        await stream.close()

        assert [c[0] for c in manager.mock_calls] == ["disconnect", "aclose"]

