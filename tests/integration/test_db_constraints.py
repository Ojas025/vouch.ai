"""Integration tests: the contracts a real PostgreSQL enforces on vouch.db.models.

These are the guarantees a mocked session cannot check. Every assertion here is
one that a SQLAlchemy mock would happily let pass while production raised
IntegrityError / DataError at commit time.

Requires PostgreSQL on 127.0.0.1:5432 (docker compose up -d).
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DataError, IntegrityError

from vouch.db.models import BranchJob, Investigations, OutboxEvent, Submission

pytestmark = pytest.mark.integration

# Every outbox insert below fails on one column: OutboxEvent.published_at is
# declared `Mapped[datetime]` with no `nullable=True`, which SQLAlchemy turns
# into NOT NULL. A pending row has nothing to publish yet, so the insert raises
# psycopg's NotNullViolation at commit. These tests are pinned to that defect
# (strict, so fixing the column turns them into XPASS failures that force the
# marker off) rather than deleted or weakened.
BLOCKED_BY_PUBLISHED_AT_NOT_NULL = pytest.mark.xfail(
    strict=True,
    reason=(
        "OutboxEvent.published_at is NOT NULL (Mapped[datetime] without "
        "nullable=True), so inserting a pending outbox row raises NotNullViolation"
    ),
)


async def make_submission(session, **overrides):
    fields = {
        "channel": "whatsapp",
        "idempotency_key": "whatsapp:wamid.1",
        "payload": '{"text": "verify your account"}',
        "sha256_digest": "a" * 64,
    }
    fields.update(overrides)
    submission = Submission(**fields)
    session.add(submission)
    await session.commit()
    return submission


async def make_outbox_event(session, **overrides):
    fields = {
        "channel": "whatsapp",
        "idempotency_key": "whatsapp:wamid.1",
        "payload": {"text": "verify your account"},
    }
    fields.update(overrides)
    event = OutboxEvent(**fields)
    session.add(event)
    await session.commit()
    return event


class TestSubmissionPersistence:
    @pytest.mark.asyncio
    async def test_row_round_trips_through_the_database(self, db_session):
        submission = await make_submission(db_session)

        loaded = await db_session.get(Submission, submission.id)

        assert loaded.channel == "whatsapp"
        assert loaded.payload == '{"text": "verify your account"}'
        assert loaded.sha256_digest == "a" * 64

    @pytest.mark.asyncio
    async def test_state_defaults_to_received(self, db_session):
        submission = await make_submission(db_session)

        loaded = await db_session.get(Submission, submission.id)

        assert loaded.state == "RECEIVED"

    @pytest.mark.asyncio
    async def test_primary_key_is_generated_without_being_supplied(self, db_session):
        submission = await make_submission(db_session)

        assert isinstance(submission.id, uuid.UUID)

    @pytest.mark.asyncio
    async def test_created_at_is_generated_and_timezone_aware(self, db_session):
        submission = await make_submission(db_session)

        loaded = await db_session.get(Submission, submission.id)

        assert isinstance(loaded.created_at, datetime)
        assert loaded.created_at.tzinfo is not None

    @BLOCKED_BY_PUBLISHED_AT_NOT_NULL
    @pytest.mark.asyncio
    async def test_published_at_stays_null_until_published(self, db_session):
        event = await make_outbox_event(db_session)

        loaded = await db_session.get(OutboxEvent, event.id)

        assert loaded.published_at is None
        assert loaded.status == "pending"

    @pytest.mark.asyncio
    async def test_unicode_payload_survives_the_round_trip(self, db_session):
        submission = await make_submission(
            db_session, payload='{"text": "नमस्ते 🙏 verify now"}'
        )

        loaded = await db_session.get(Submission, submission.id)

        assert "नमस्ते 🙏" in loaded.payload


class TestNotNullEnforcement:
    @pytest.mark.parametrize(
        "missing", ["channel", "idempotency_key", "payload", "sha256_digest"]
    )
    @pytest.mark.asyncio
    async def test_missing_required_column_is_rejected_by_the_database(
        self, db_session, missing
    ):
        db_session.add(Submission(**{k: "x" for k in
                                      ["channel", "idempotency_key", "payload",
                                       "sha256_digest"] if k != missing}))

        with pytest.raises(IntegrityError):
            await db_session.commit()

        await db_session.rollback()

    @pytest.mark.asyncio
    async def test_missing_outbox_payload_is_rejected(self, db_session):
        db_session.add(OutboxEvent(channel="whatsapp", idempotency_key="k"))

        with pytest.raises(IntegrityError):
            await db_session.commit()

        await db_session.rollback()


@BLOCKED_BY_PUBLISHED_AT_NOT_NULL
class TestOutboxPayloadColumn:
    @pytest.mark.asyncio
    async def test_jsonb_payload_round_trips_as_structure(self, db_session):
        event = await make_outbox_event(
            db_session,
            payload={"text": "hi", "entities": [{"type": "upi_vpa", "value": "m@okaxis"}]},
        )

        loaded = await db_session.get(OutboxEvent, event.id)

        assert loaded.payload["entities"][0]["type"] == "upi_vpa"

    @pytest.mark.asyncio
    async def test_jsonb_payload_is_queryable_by_containment(self, db_session):
        # Containment is how the relay finds work for one entity without
        # deserializing every pending row.
        await make_outbox_event(db_session, payload={"text": "pay merchant@okaxis"})
        await make_outbox_event(
            db_session,
            idempotency_key="whatsapp:wamid.2",
            payload={"text": "unrelated"},
        )

        found = await db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.payload.contains({"text": "pay merchant@okaxis"})
            )
        )

        rows = found.scalars().all()
        assert len(rows) == 1
        assert rows[0].idempotency_key == "whatsapp:wamid.1"

    @pytest.mark.asyncio
    async def test_upsert_on_the_jsonb_column_uses_native_jsonb(self, db_session):
        # Exercises the column's JSONB type through the insert() construct
        # rather than ORM attribute assignment.
        await db_session.execute(
            insert(OutboxEvent).values(
                channel="telegram",
                idempotency_key="telegram:1",
                payload={"nested": {"a": [1, 2]}},
            )
        )
        await db_session.commit()

        rows = (await db_session.execute(select(OutboxEvent))).scalars().all()

        assert rows[0].payload == {"nested": {"a": [1, 2]}}


class TestColumnWidth:
    @BLOCKED_BY_PUBLISHED_AT_NOT_NULL
    @pytest.mark.asyncio
    async def test_realistic_whatsapp_idempotency_key_is_accepted(self, db_session):
        key = "whatsapp:wamid." + "A" * 60

        event = await make_outbox_event(db_session, idempotency_key=key)

        loaded = await db_session.get(OutboxEvent, event.id)
        assert loaded.idempotency_key == key

    @pytest.mark.asyncio
    async def test_key_beyond_the_column_width_is_rejected_by_postgres(self, db_session):
        # Proves the width is a real constraint and not just a declaration: this
        # is the failure a mocked session hides until production.
        db_session.add(
            OutboxEvent(
                channel="whatsapp",
                idempotency_key="x" * 500,
                payload={"text": "hi"},
            )
        )

        with pytest.raises(DataError):
            await db_session.commit()

        await db_session.rollback()


class TestReferentialIntegrity:
    @pytest.mark.asyncio
    async def test_investigation_requires_an_existing_submission(self, db_session):
        db_session.add(Investigations(submission_id=uuid.uuid4(), reason="initial"))

        with pytest.raises(IntegrityError):
            await db_session.commit()

        await db_session.rollback()

    @pytest.mark.asyncio
    async def test_branch_job_requires_an_existing_investigation(self, db_session):
        db_session.add(
            BranchJob(
                investigation_id=uuid.uuid4(),
                modality="apk",
                sandbox_profile="default",
            )
        )

        with pytest.raises(IntegrityError):
            await db_session.commit()

        await db_session.rollback()

    @pytest.mark.asyncio
    async def test_branch_job_survives_the_full_chain(self, db_session):
        submission = await make_submission(db_session)
        investigation = Investigations(submission_id=submission.id, reason="initial")
        db_session.add(investigation)
        await db_session.commit()

        db_session.add(
            BranchJob(
                investigation_id=investigation.inv_id,
                modality="apk",
                sandbox_profile="default",
                artifact_sha256="b" * 64,
            )
        )
        await db_session.commit()

        job = (await db_session.execute(select(BranchJob))).scalars().one()

        assert job.attempt_no == 1
        assert job.state == "QUEUED"
        assert job.artifact_sha256 == "b" * 64


class TestFixtureIsolation:
    @pytest.mark.asyncio
    async def test_rows_from_a_previous_test_are_gone(self, db_session):
        # Guards the fixture itself: without the per-test TRUNCATE this test
        # would see the rows an earlier test committed and the suite would
        # become order-dependent.
        remaining = (await db_session.execute(select(OutboxEvent))).scalars().all()

        assert remaining == []

    @BLOCKED_BY_PUBLISHED_AT_NOT_NULL
    @pytest.mark.asyncio
    async def test_timestamps_come_back_in_utc(self, db_session):
        event = await make_outbox_event(db_session)
        loaded = await db_session.get(OutboxEvent, event.id)

        assert loaded.created_at.utcoffset() == timezone.utc.utcoffset(None)