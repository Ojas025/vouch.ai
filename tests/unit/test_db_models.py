"""Unit tests for the SQLAlchemy ORM declarations in vouch.db.models.

These assert the *shape* of the schema the rest of the system is written
against: table names, columns, primary keys, foreign keys and column types.
They run without a database; the constraints a real PostgreSQL enforces on top
of this shape (NOT NULL, JSONB round-trip, VARCHAR length, server defaults) are
covered by tests/integration/test_db_constraints.py.
"""

import pytest
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.sqltypes import UUID as SQLUUID

from vouch.db.models import BranchJob, Investigations, OutboxEvent, Submission
from vouch.utils import uuid7


class TestTableNames:
    @pytest.mark.parametrize(
        ("model", "table_name"),
        [
            (Submission, "submissions"),
            (OutboxEvent, "outbox_events"),
            (Investigations, "investigations"),
            (BranchJob, "branch_jobs"),
        ],
    )
    def test_declared_table_name(self, model, table_name):
        assert model.__tablename__ == table_name


class TestColumns:
    @pytest.mark.parametrize(
        "column",
        ["id", "payload", "sha256_digest", "state", "channel",
         "idempotency_key", "created_at", "updated_at"],
    )
    def test_submission_has_column(self, column):
        assert column in Submission.__table__.c

    @pytest.mark.parametrize(
        "column",
        ["id", "channel", "idempotency_key", "payload", "status",
         "created_at", "published_at"],
    )
    def test_outbox_event_has_column(self, column):
        assert column in OutboxEvent.__table__.c

    @pytest.mark.parametrize(
        "column", ["inv_id", "submission_id", "reason", "state", "created_at"]
    )
    def test_investigation_has_column(self, column):
        assert column in Investigations.__table__.c

    @pytest.mark.parametrize(
        "column",
        ["job_id", "investigation_id", "modality", "artifact_sha256", "url",
         "sandbox_profile", "attempt_no", "state"],
    )
    def test_branch_job_has_column(self, column):
        assert column in BranchJob.__table__.c


class TestPrimaryKeys:
    @pytest.mark.parametrize(
        ("model", "pk"),
        [
            (Submission, "id"),
            (OutboxEvent, "id"),
            (Investigations, "inv_id"),
            (BranchJob, "job_id"),
        ],
    )
    def test_primary_key_is_a_uuid_column(self, model, pk):
        column = model.__table__.c[pk]

        assert isinstance(column.type, SQLUUID)
        assert column.primary_key is True

    @pytest.mark.parametrize(
        ("model", "pk"),
        [
            (Submission, "id"),
            (OutboxEvent, "id"),
            (Investigations, "inv_id"),
            (BranchJob, "job_id"),
        ],
    )
    def test_primary_key_is_generated_by_uuid7(self, model, pk):
        # Application-side generation: an insert never supplies the id.
        # SQLAlchemy wraps a zero-arg callable default, so unwrap to compare.
        generator = model.__table__.c[pk].default.arg
        assert getattr(generator, "__wrapped__", generator) is uuid7


class TestForeignKeys:
    def test_investigation_points_at_submissions(self):
        fks = Investigations.__table__.c.submission_id.foreign_keys

        assert [fk.target_fullname for fk in fks] == ["submissions.id"]

    def test_branch_job_points_at_investigations(self):
        fks = BranchJob.__table__.c.investigation_id.foreign_keys

        assert [fk.target_fullname for fk in fks] == ["investigations.inv_id"]


class TestColumnTypes:
    def test_submission_payload_is_text(self):
        assert isinstance(Submission.__table__.c.payload.type, Text)

    def test_outbox_payload_is_jsonb(self):
        # The relay copies payload verbatim into the event envelope, so it must
        # be JSONB rather than text that needs re-parsing on both sides.
        assert isinstance(OutboxEvent.__table__.c.payload.type, JSONB)

    def test_submission_digest_column_has_no_fixed_length(self):
        # A 64-char hex digest must never be truncated by the column type.
        assert Submission.__table__.c.sha256_digest.type.length is None


class TestOutboxIdempotencyKeyWidth:
    def test_column_holds_a_realistic_whatsapp_key(self):
        column = OutboxEvent.__table__.c.idempotency_key
        realistic_key = "whatsapp:" + "wamid." + "A" * 60

        assert column.type.length is not None
        assert len(realistic_key) <= column.type.length

    def test_column_holds_a_realistic_telegram_key(self):
        column = OutboxEvent.__table__.c.idempotency_key
        telegram_key = "telegram:" + str(10**18)

        assert column.type.length is not None
        assert len(telegram_key) <= column.type.length