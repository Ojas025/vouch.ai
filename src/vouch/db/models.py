from datetime import datetime, timezone
import uuid

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import UUID
from sqlalchemy.dialects.postgresql import JSONB

from vouch.db.base import Base


def _uuid7() -> uuid.UUID:
    """Time-ordered UUID stand-in. ``uuid.uuid7`` is Python 3.14+; this project is on 3.13.

    Only the import-time blocker is fixed here - the value semantics (ordering,
    version/variant bits) are deliberately left alone pending a real decision.
    """
    return uuid.uuid4()

class Submission(Base):
    __tablename__ = "submissions"

    # Primary Key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=_uuid7,
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
        default=_uuid7,
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
