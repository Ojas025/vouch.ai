from datetime import datetime, timezone
import uuid

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from vouch.db.base import Base

class Submission(Base):
    __tablename__ = "submissions"

    # Primary Key
    id: Mapped[int] = mapped_column(
        primary_key=True,
    )

    payload: Mapped[str] = mapped_column(Text)
    sha256_digest: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String(20), server_default="RECEIVED")

    channel: str
    idempotency_key: str

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now()
    )
