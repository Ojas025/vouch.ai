from datetime import datetime
from enum import Enum
import uuid

from sqlalchemy import (
    Boolean,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
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
    idempotency_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSONB)

    # W3C context of the request that created this row; null on older rows.
    trace_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

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

    investigation_id: Mapped[uuid.UUID] = mapped_column(
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

class Ledger(Base):
    __tablename__ = "evidence_ledger"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('source_observation', 'interpretation')",
            name="ck_evidence_ledger_kind",
        ),
        CheckConstraint(
            "length(finding) BETWEEN 1 AND 1200",
            name="ck_evidence_ledger_finding_length",
        ),
        CheckConstraint(
            "trust IN ('system', 'deterministic', 'external_source', 'untrusted_artifact', 'model_output')",
            name="ck_evidence_ledger_trust",
        ),
        CheckConstraint(
            "producer_type IN ('AGENT', 'SERVICE', 'BRANCH')",
            name="ck_evidence_ledger_producer_type",
        ),
        CheckConstraint(
            "artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_evidence_ledger_artifact_sha256",
        ),
        CheckConstraint(
            "ledger_seq > 0",
            name="ck_evidence_ledger_ledger_seq",
        ),
        UniqueConstraint(
            "investigation_id",
            "dedupe_key",
            name="uq_evidence_ledger_dedupe",
        ),
        Index(
            "idx_evidence_ledger_watermark",
            "investigation_id",
            "ledger_seq",
        ),
        Index(
            "idx_evidence_ledger_entity_ids",
            "entity_ids",
            postgresql_using="gin",
            postgresql_ops={"entity_ids": "jsonb_path_ops"},
        ),
    )

    evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.investigation_id"),
        nullable=False,
    )

    ledger_seq: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=False),
        nullable=False,
    )

    kind: Mapped[str] = mapped_column(String(20), nullable=False)

    finding: Mapped[str] = mapped_column(Text, nullable=False)

    trust: Mapped[str] = mapped_column(String(30), nullable=False)

    producer_type: Mapped[str] = mapped_column(String(10), nullable=False)
    producer_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sandbox_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("artifacts.sha256_digest"),
        nullable=True,
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    supersedes: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evidence_ledger.evidence_id"),
        nullable=True,
    )

    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)

    entity_ids: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

class Origin(Enum):
    ORIGINAL = "original"
    DERIVED = "derived"

class Artifacts(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            "sha256_digest ~ '^[0-9a-f]{64}$'",
            name="ck_artifacts_sha256_digest",
        ),
        CheckConstraint(
            "(origin = 'ORIGINAL' AND derived_from IS NULL)"
            " OR (origin = 'DERIVED' AND derived_from IS NOT NULL)",
            name="ck_artifacts_origin_derived_from",
        ),
        Index(
            "idx_artifacts_investigation",
            "investigation_id",
        ),
    )

    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True
    )

    sha256_digest: Mapped[str] = mapped_column(
        String,
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.investigation_id"),
    )

    submission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("submissions.id"),
    )

    origin: Mapped[Origin]

    modality: Mapped[str] = mapped_column(String(20), nullable=False)

    derived_from: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("artifacts.sha256_digest"),
        nullable=True,
    )

    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    file_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    provider_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    storage_uri: Mapped[str | None] = mapped_column(Text, nullable=True)

    digest_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

class ToolCalls(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN ("
            "'tool_not_found', 'unauthorized_tool_access', 'rate_limit_reached', "
            "'invalid_arguments', 'budget_exceeded', 'tool_timeout', 'tool_error', "
            "'output_validation_failed', 'approval_rejected', 'network_disallowed')",
            name="ck_tool_calls_failure_reason",
        ),
        CheckConstraint(
            "(is_error = false AND result IS NOT NULL AND failure_reason IS NULL)"
            " OR (is_error = true AND result IS NULL)",
            name="ck_tool_calls_terminal_state",
        ),
        Index(
            "idx_tool_calls_investigation",
            "investigation_id",
            "completed_at",
        ),
        Index(
            "idx_tool_calls_task",
            "task_id",
            postgresql_where=text("task_id IS NOT NULL"),
        ),
    )

    tool_invocation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
    )

    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.investigation_id"),
        nullable=False,
    )

    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )

    agent_role: Mapped[str] = mapped_column(String(30), nullable=False)

    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(16), nullable=False)

    arguments: Mapped[dict] = mapped_column(JSONB, nullable=False)

    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result_truncated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

class Entities(Base):
    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ("
            "'phone', 'upi_vpa', 'ifsc', 'amount', 'url', 'email', 'apk_package', "
            "'file_sha256', 'brand', 'name', 'domain', 'ipv4', 'ipv6', "
            "'certificate_fingerprint', 'other')",
            name="ck_entities_entity_type",
        ),
        UniqueConstraint(
            "entity_type",
            "normalized_value",
            name="uq_entities_type_value",
        ),
    )

    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid7,
    )

    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)

    normalized_value: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
