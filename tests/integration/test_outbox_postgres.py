"""Integration tests: the outbox relay's locking and transition semantics on real
PostgreSQL.

`FOR UPDATE SKIP LOCKED` is the whole reason a relay can run on more than one
instance: it must let the second relay skip rows the first is still holding,
rather than block on them or double-publish them. A mock session cannot express
any of that, so every assertion here runs against a real database with two real
concurrent transactions.

Requires PostgreSQL on 127.0.0.1:5432 (docker compose up -d).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from vouch.api.outbox import publish_outbox
from vouch.db.models import OutboxEvent

pytestmark = pytest.mark.integration

# The relay's rows cannot be created at all while OutboxEvent.published_at is
# NOT NULL: every insert below dies with NotNullViolation at commit, so none of
# the locking or transition semantics are reachable. Pinned strict so that
# making the column nullable turns these into XPASS failures.
BLOCKED_BY_PUBLISHED_AT_NOT_NULL = pytest.mark.xfail(
    strict=True,
    reason=(
        "OutboxEvent.published_at is NOT NULL, so a pending row cannot be inserted "
        "and the relay has nothing to select"
    ),
)

BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class RecordingStream:
    """Broker double: records the Events the relay produces."""

    def __init__(self, fail_on: int | None = None):
        self.produced = []
        self.fail_on = fail_on

    async def produce(self, event):
        if self.fail_on is not None and len(self.produced) == self.fail_on:
            raise RuntimeError("broker down")
        self.produced.append(event)


async def add_event(session, key: str, created_at: datetime, status: str = "pending"):
    event = OutboxEvent(
        channel="whatsapp",
        idempotency_key=key,
        payload={"text": f"payload for {key}"},
        status=status,
        created_at=created_at,
    )
    session.add(event)
    await session.commit()
    return event


async def statuses(session):
    rows = (await session.execute(select(OutboxEvent.idempotency_key, OutboxEvent.status))).all()
    return dict(rows)


@BLOCKED_BY_PUBLISHED_AT_NOT_NULL
class TestRowSelection:
    @pytest.mark.asyncio
    async def test_publishes_pending_rows_and_maps_them_to_events(
        self, db_session, db_sessionmaker
    ):
        await add_event(db_session, "whatsapp:1", BASE_TIME)

        stream = RecordingStream()
        await publish_outbox(db_session, stream)

        assert [e.idempotency_key for e in stream.produced] == ["whatsapp:1"]
        assert stream.produced[0].channel == "whatsapp"
        assert stream.produced[0].payload == {"text": "payload for whatsapp:1"}

    @pytest.mark.asyncio
    async def test_already_published_rows_are_never_republished(
        self, db_session
    ):
        await add_event(db_session, "whatsapp:1", BASE_TIME, status="published")

        stream = RecordingStream()
        await publish_outbox(db_session, stream)

        assert stream.produced == []

    @pytest.mark.asyncio
    async def test_publishes_in_creation_order(self, db_session):
        await add_event(db_session, "whatsapp:3", BASE_TIME + timedelta(minutes=3))
        await add_event(db_session, "whatsapp:1", BASE_TIME + timedelta(minutes=1))
        await add_event(db_session, "whatsapp:2", BASE_TIME + timedelta(minutes=2))

        stream = RecordingStream()
        await publish_outbox(db_session, stream)

        assert [e.idempotency_key for e in stream.produced] == [
            "whatsapp:1", "whatsapp:2", "whatsapp:3"
        ]


@BLOCKED_BY_PUBLISHED_AT_NOT_NULL
class TestBatchLimit:
    @pytest.mark.asyncio
    async def test_publishes_at_most_ten_rows_per_pass(self, db_session):
        for i in range(12):
            await add_event(db_session, f"whatsapp:{i}", BASE_TIME + timedelta(minutes=i))

        stream = RecordingStream()
        await publish_outbox(db_session, stream)

        published = [e.idempotency_key for e in stream.produced]
        assert len(published) == 10
        assert published == [f"whatsapp:{i}" for i in range(10)]
        # The two that missed the batch must still be pending for the next pass.
        assert (await statuses(db_session))["whatsapp:10"] == "pending"
        assert (await statuses(db_session))["whatsapp:11"] == "pending"

    @pytest.mark.asyncio
    async def test_the_next_pass_publishes_the_remainder(self, db_session):
        for i in range(12):
            await add_event(db_session, f"whatsapp:{i}", BASE_TIME + timedelta(minutes=i))

        await publish_outbox(db_session, RecordingStream())
        await publish_outbox(db_session, RecordingStream())

        assert set((await statuses(db_session)).values()) == {"published"}


@BLOCKED_BY_PUBLISHED_AT_NOT_NULL
class TestSkipLockedConcurrency:
    @pytest.mark.asyncio
    async def test_second_relay_skips_rows_the_first_is_holding(
        self, db_session, db_sessionmaker
    ):
        for i in range(5):
            await add_event(db_session, f"whatsapp:{i}", BASE_TIME + timedelta(minutes=i))

        # Relay A selects and holds the row locks (no commit yet).
        relay_a = db_sessionmaker()
        async with relay_a as session_a:
            rows_a = (
                await session_a.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.status == "pending")
                    .order_by(OutboxEvent.created_at)
                    .limit(10)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            assert len(rows_a) == 5

            # Relay B runs the identical select while A still holds the locks.
            relay_b = db_sessionmaker()
            async with relay_b as session_b:
                rows_b = (
                    await session_b.execute(
                        select(OutboxEvent)
                        .where(OutboxEvent.status == "pending")
                        .order_by(OutboxEvent.created_at)
                        .limit(10)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().all()

            # SKIP LOCKED, not "wait": B gets nothing back and is not blocked.
            assert rows_b == []

    @pytest.mark.asyncio
    async def test_rows_become_available_once_the_holder_commits(
        self, db_session, db_sessionmaker
    ):
        await add_event(db_session, "whatsapp:1", BASE_TIME)

        relay_a = db_sessionmaker()
        async with relay_a as session_a:
            rows_a = (
                await session_a.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.status == "pending")
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()
            assert len(rows_a) == 1

            stream_a = RecordingStream()
            await publish_outbox(session_a, stream_a)

            assert [e.idempotency_key for e in stream_a.produced] == ["whatsapp:1"]

        # A committed: the next relay sees the row as already published, not pending.
        stream_b = RecordingStream()
        await publish_outbox(db_session, stream_b)

        assert stream_b.produced == []
        assert (await statuses(db_session))["whatsapp:1"] == "published"


@BLOCKED_BY_PUBLISHED_AT_NOT_NULL
class TestTransitions:
    @pytest.mark.asyncio
    async def test_published_rows_carry_a_utc_timestamp(self, db_session):
        await add_event(db_session, "whatsapp:1", BASE_TIME)

        await publish_outbox(db_session, RecordingStream())

        event = (await db_session.execute(select(OutboxEvent))).scalars().one()
        assert event.status == "published"
        assert event.published_at is not None
        assert event.published_at.utcoffset() == timedelta(0)

    @pytest.mark.asyncio
    async def test_broker_failure_commits_nothing(self, db_session):
        for i in range(3):
            await add_event(db_session, f"whatsapp:{i}", BASE_TIME + timedelta(minutes=i))

        # Fails on the second event: the first was published to the broker, so
        # nothing may be marked — the next pass must republish everything.
        with pytest.raises(RuntimeError, match="broker down"):
            await publish_outbox(db_session, RecordingStream(fail_on=1))

        await db_session.rollback()

        assert set((await statuses(db_session)).values()) == {"pending"}

    @pytest.mark.asyncio
    async def test_recovery_after_a_broker_failure_publishes_everything(
        self, db_session
    ):
        for i in range(3):
            await add_event(db_session, f"whatsapp:{i}", BASE_TIME + timedelta(minutes=i))

        with pytest.raises(RuntimeError):
            await publish_outbox(db_session, RecordingStream(fail_on=1))
        await db_session.rollback()

        stream = RecordingStream()
        await publish_outbox(db_session, stream)

        assert len(stream.produced) == 3
        assert set((await statuses(db_session)).values()) == {"published"}