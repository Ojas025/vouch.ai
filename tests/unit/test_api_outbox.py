"""Unit tests for vouch.api.outbox.publish_outbox and outbox_worker.

The AsyncSession is an AsyncMock, so no PostgreSQL connection is opened. The
contract under test is which rows get selected, that each is published exactly
once, and that the status/published_at transition only happens on success.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from vouch.api import outbox as outbox_module
from vouch.api.outbox import outbox_worker, publish_outbox
from vouch.db.models import OutboxEvent


def _compile(stmt):
    """Render a statement the way PostgreSQL will see it."""
    return str(stmt.compile(dialect=postgresql.dialect()))


def make_event(idempotency_key="whatsapp:wamid.1", status=None, created_at=None):
    event = MagicMock(spec=OutboxEvent)
    event.id = f"id-{idempotency_key}"
    event.channel = "whatsapp"
    event.idempotency_key = idempotency_key
    event.payload = {"text": "hi"}
    event.status = status
    event.published_at = None
    event.created_at = created_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return event


@pytest.fixture
def session():
    s = AsyncMock()
    s.execute = AsyncMock()
    s.commit = AsyncMock()
    s.scalars = MagicMock()
    return s


@pytest.fixture
def stream():
    s = AsyncMock()

    async def _validating_produce(payload):
        if isinstance(payload, OutboxEvent):
            raise TypeError(
                "Object of type OutboxEvent is not JSON serializable: "
                "publish_outbox must map ORM rows to Event before producing."
            )

    s.produce = AsyncMock(side_effect=_validating_produce)
    return s


@pytest.fixture
def results(session):
    """Session whose execute() returns the given ORM rows."""
    def _set(events):
        result = MagicMock()
        result.scalars.return_value.all.return_value = events
        session.execute.return_value = result
    return _set


class TestPublishOutboxRelaySeam:
    @pytest.mark.asyncio
    async def test_publishes_real_rows_through_a_real_event_stream(self, session):
        """The relay's whole job: hand selected rows to EventStream.produce.

        Uses a real EventStream (mock Redis client only) and a real
        OutboxEvent row, so the row -> json.dumps contract is actually
        exercised rather than absorbed by MagicMock(spec=OutboxEvent).
        """
        from vouch.queue.stream import EventStream

        row = OutboxEvent(idempotency_key="telegram:1", payload={"text": "hi"})
        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        session.execute.return_value = result

        stream = EventStream(redis_url="redis://localhost:6379/15", stream_name="events:stream")
        stream.client = AsyncMock()

        await publish_outbox(session, stream)

        stream.client.xadd.assert_awaited_once()
        sent = stream.client.xadd.call_args.args[1]["data"]
        assert "telegram:1" in sent
        assert row.status == "published"
        assert row.published_at is not None


class TestPublishOutboxSelection:
    @pytest.mark.asyncio
    async def test_selects_only_pending_events(self, session, stream, results):
        # Asserts the literal bound parameter, not a substring: a mutation from
        # status == "pending" to != "published" must fail here.
        results([make_event()])

        await publish_outbox(session, stream)

        stmt = session.execute.call_args.args[0]
        assert "status" in str(stmt.whereclause)
        assert "pending" in stmt.compile().params.values()

    @pytest.mark.asyncio
    async def test_orders_by_creation_ascending(self, session, stream, results):
        # Asserts the ordered column and direction. A mutation to
        # published_at, or to .desc(), must fail here.
        results([make_event()])

        await publish_outbox(session, stream)

        stmt = session.execute.call_args.args[0]
        compiled = _compile(stmt)
        order_by = compiled.split("ORDER BY")[1].split("LIMIT")[0]
        assert "created_at" in order_by
        assert "DESC" not in order_by.upper()

    @pytest.mark.asyncio
    async def test_limits_to_a_batch_of_ten(self, session, stream, results):
        # The batch size is an operational parameter (relay throughput), so the
        # exact value is asserted rather than the presence of "LIMIT".
        results([])

        await publish_outbox(session, stream)

        stmt = session.execute.call_args.args[0]
        assert stmt._limit_clause is not None
        assert stmt._limit_clause.value == 10

    @pytest.mark.asyncio
    async def test_uses_skip_locked_for_concurrent_relays(self, session, stream, results):
        results([])

        await publish_outbox(session, stream)

        # FOR UPDATE SKIP LOCKED keeps two relays from claiming the same rows.
        # It only renders under the PostgreSQL dialect, hence _compile().
        assert "FOR UPDATE SKIP LOCKED" in _compile(session.execute.call_args.args[0])

    @pytest.mark.asyncio
    async def test_empty_queue_publishes_nothing_but_still_commits(
        self, session, stream, results
    ):
        results([])

        await publish_outbox(session, stream)

        stream.produce.assert_not_awaited()
        session.commit.assert_awaited_once()


class TestPublishOutboxTransitions:
    @pytest.mark.asyncio
    async def test_publishes_each_pending_event(self, session, stream, results):
        events = [make_event("telegram:1"), make_event("telegram:2")]
        results(events)

        await publish_outbox(session, stream)

        # The relay maps each ORM row to a serialisable Event, so the broker
        # sees the row's fields rather than the row object itself.
        assert stream.produce.await_count == 2
        produced = [c.args[0] for c in stream.produce.call_args_list]
        assert [e.idempotency_key for e in produced] == ["telegram:1", "telegram:2"]
        assert [e.payload for e in produced] == [{"text": "hi"}, {"text": "hi"}]

    @pytest.mark.asyncio
    async def test_marks_published_with_utc_timestamp(self, session, stream, results):
        event = make_event()
        results([event])

        await publish_outbox(session, stream)

        assert event.status == "published"
        assert isinstance(event.published_at, datetime)
        assert event.published_at.tzinfo is not None

    @pytest.mark.asyncio
    async def test_commits_once_after_all_events(self, session, stream, results):
        results([make_event("a"), make_event("b"), make_event("c")])

        await publish_outbox(session, stream)

        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_publishes_before_marking(self, session, stream, results):
        event = make_event()
        results([event])
        seen = []
        # Capture the row's status at the moment the broker call happens: it
        # must still be unset, proving publish precedes the transition.
        stream.produce.side_effect = lambda e: seen.append(event.status)

        await publish_outbox(session, stream)

        # Status is still unset while the broker call is in flight.
        assert seen == [None]


class TestPublishOutboxFailures:
    @pytest.mark.asyncio
    async def test_propagates_broker_failure_without_marking_published(
        self, session, stream, results
    ):
        event = make_event()
        results([event])
        stream.produce.side_effect = RuntimeError("broker down")

        with pytest.raises(RuntimeError, match="broker down"):
            await publish_outbox(session, stream)

        assert event.status != "published"
        assert event.published_at is None
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stops_at_first_failure_leaving_later_events_untouched(
        self, session, stream, results
    ):
        first, second = make_event("a"), make_event("b")
        results([first, second])
        stream.produce.side_effect = [RuntimeError("down"), None]

        with pytest.raises(RuntimeError):
            await publish_outbox(session, stream)

        assert stream.produce.await_count == 1
        assert second.status != "published"

    @pytest.mark.asyncio
    async def test_propagates_select_failure(self, session, stream):
        session.execute.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError, match="db down"):
            await publish_outbox(session, stream)

        stream.produce.assert_not_awaited()
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_propagates_commit_failure(self, session, stream, results):
        results([make_event()])
        session.commit.side_effect = RuntimeError("commit failed")

        with pytest.raises(RuntimeError, match="commit failed"):
            await publish_outbox(session, stream)


class TestOutboxEventSchema:
    def test_idempotency_key_fits_a_realistic_whatsapp_key(self):
        # WhatsApp wamid keys are 40-60+ characters; with the "whatsapp:"
        # prefix a String(30) column raises DataError at commit against a real
        # PostgreSQL — a 500 on every webhook that every mocked session hides.
        column = OutboxEvent.__table__.c.idempotency_key
        realistic_key = "whatsapp:wamid." + "A" * 60

        assert column.type.length is not None
        assert len(realistic_key) <= column.type.length


class TestOutboxWorkerLoop:
    @pytest.mark.asyncio
    async def test_polls_repeatedly_then_stops_on_cancel(self, stream):
        async def session_ctx():
            s = AsyncMock()
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            s.execute.return_value = result
            return s

        sessions = [await session_ctx()]

        def factory():
            return sessions[0]

        with patch.object(outbox_module, "SessionLocal", factory):
            with patch.object(outbox_module, "publish_outbox", AsyncMock()) as publish:
                publish.side_effect = [None, None, asyncio.CancelledError()]

                with pytest.raises(asyncio.CancelledError):
                    await outbox_worker(stream)

        assert publish.await_count == 3

    @pytest.mark.asyncio
    async def test_sleeps_between_polls(self, stream):
        with patch.object(outbox_module, "SessionLocal") as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            factory.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch.object(outbox_module, "publish_outbox", AsyncMock()):
                with patch.object(outbox_module, "sleep", AsyncMock()) as sleep:
                    sleep.side_effect = [None, asyncio.CancelledError()]
                    with pytest.raises(asyncio.CancelledError):
                        await outbox_worker(stream)

        assert sleep.await_count == 2
        assert sleep.await_args.args[0] == 2

    @pytest.mark.asyncio
    async def test_opens_a_fresh_session_per_iteration(self, stream):
        with patch.object(outbox_module, "SessionLocal") as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            factory.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch.object(outbox_module, "publish_outbox", AsyncMock()):
                with patch.object(outbox_module, "sleep", AsyncMock()):
                    sleep = outbox_module.sleep
                    sleep.side_effect = [None, None, asyncio.CancelledError()]
                    with pytest.raises(asyncio.CancelledError):
                        await outbox_worker(stream)

        assert factory.call_count >= 3

    @pytest.mark.asyncio
    async def test_propagates_publish_failure_and_stops_polling(self, stream):
        with patch.object(outbox_module, "SessionLocal") as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            factory.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch.object(
                outbox_module, "publish_outbox", AsyncMock(side_effect=RuntimeError("x"))
            ):
                with patch.object(outbox_module, "sleep", AsyncMock()) as sleep:
                    with pytest.raises(RuntimeError):
                        await outbox_worker(stream)

        sleep.assert_not_awaited()
