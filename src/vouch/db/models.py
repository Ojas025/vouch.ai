from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import UUID
from sqlalchemy.dialects.postgresql import JSONB

from vouch.db.base import Base
from vouch.utils import uuid7

"""
    Submission (case file)
        Investigation xN | why: "initial", "re-evidence", "manual"
            BranchJob xN | per modality, per artifact
                attempt xN | retry, wall-clock, sandbox violation
"""

class Submission(Base):
    __tablename__ = "submissions"

    # Primary Key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
    )

    payload: Mapped[str] = mapped_column(Text)
    sha256_digest: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String(20), server_default="RECEIVED")

    channel: Mapped[str]
    idempotency_key: Mapped[str]

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )

class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
    )

    channel: Mapped[str] = mapped_column(String(20))
    idempotency_key: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSONB)

    status: Mapped[str] = mapped_column(
        String(30),
        default="pending",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True)
    )

class Investigations(Base):
    __tablename__ = "investigations"

    inv_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
    )

    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("submissions.id"),
        index=True,
    )

    reason: Mapped[str] = mapped_column(String(20))  # "initial" | "re-evidence" | "manual"
    state: Mapped[str] = mapped_column(String(20), server_default="RECEIVED")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

class BranchJob(Base):
    __tablename__ = "branch_jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.inv_id"),
        index=True,
    )

    modality: Mapped[str] = mapped_column(String(20))
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    sandbox_profile: Mapped[str] = mapped_column(String(50))

    attempt_no: Mapped[int] = mapped_column(default=1)
    state: Mapped[str] = mapped_column(String(20), server_default="QUEUED")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
