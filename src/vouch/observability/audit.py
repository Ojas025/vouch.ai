"""Security audit events for tool calls: who tried what, and what happened.

``Auditor.emit`` is the only emission path, so severity, outcome and validation
cannot drift between call sites. Nothing here is tamper-proof or non-repudiable.
"""

import logging
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict

from vouch.logger import AUDIT_LOGGER_LEVEL, QUEUE_HANDLER_NAME, get_audit_logger
from vouch.observability.context import bind_correlation
from vouch.observability.sanitize import sanitize
from vouch.tools.models import DecisionEffect, ToolFailureReason, ToolSideEffects
from vouch.utils import uuid7


class AuditEvent(StrEnum):
    """The finite taxonomy of audit events. Values, not names, carry the dot."""

    TOOL_CALL_REQUESTED = "tool_call.requested"
    TOOL_CALL_AUTHORIZATION_DECIDED = "tool_call.authorization_decided"
    TOOL_CALL_EXECUTION_STARTED = "tool_call.execution_started"
    TOOL_CALL_COMPLETED = "tool_call.completed"
    TOOL_CALL_FAILED = "tool_call.failed"
    TOOL_CALL_TIMED_OUT = "tool_call.timed_out"
    TOOL_CALL_CANCELLED = "tool_call.cancelled"


class AuditEmitStatus(StrEnum):
    """How far an audit record actually got.

    ``ENQUEUED`` means handed to the logging API and queued, not written or
    persisted. ``FAILED`` means constructed and validated but delivery raised.
    """

    ENQUEUED = "enqueued"
    EMBEDDED = "embedded"
    SKIPPED = "skipped"
    FAILED = "failed"


class AuditEventPolicy(NamedTuple):
    """Canonical semantics for one event. Callers never supply these."""

    severity: str
    outcome: str | None
    #: Whether "did dispatch actually happen" is meaningful for this event.
    dispatched_applicable: bool
    #: Unreachable unless the handler was invoked; False for ``failed``, which covers both.
    dispatch_must_have_occurred: bool
    #: Events that cannot be interpreted without a stable reason code.
    requires_reason: bool


_NOT_DISPATCHED = AuditEventPolicy("INFO", None, False, False, False)
#: Single source of truth for event severity, outcome and required fields.
EVENT_POLICIES: dict[AuditEvent, AuditEventPolicy] = {
    AuditEvent.TOOL_CALL_REQUESTED: _NOT_DISPATCHED,
    AuditEvent.TOOL_CALL_AUTHORIZATION_DECIDED: _NOT_DISPATCHED,
    AuditEvent.TOOL_CALL_EXECUTION_STARTED: AuditEventPolicy("INFO", None, True, True, False),
    AuditEvent.TOOL_CALL_COMPLETED: AuditEventPolicy("INFO", "success", True, True, False),
    # Covers both "never dispatched" and "dispatched and blew up".
    AuditEvent.TOOL_CALL_FAILED: AuditEventPolicy("WARNING", "failure", True, False, True),
    AuditEvent.TOOL_CALL_TIMED_OUT: AuditEventPolicy("WARNING", "timeout", True, True, True),
    # No reason: cancellation is not a ToolFailureReason and tool_error would misreport it.
    AuditEvent.TOOL_CALL_CANCELLED: AuditEventPolicy("WARNING", "cancelled", True, True, False),
}


class ToolCallAuditRecord(BaseModel):
    """The payload of one tool-call audit event."""

    model_config = ConfigDict(extra="forbid")

    event: AuditEvent
    audit_event_id: str
    occurred_at: datetime
    tool_call_id: str
    tool_name: str | None = None
    #: ToolDefinition.side_effects is this domain's category of tool.
    tool_side_effects: ToolSideEffects | None = None
    tool_version: str | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    authorization_decision: DecisionEffect | None = None
    policy_id: str | None = None
    policy_version: str | None = None
    reason_code: ToolFailureReason | None = None
    error_category: str | None = None
    dispatched: bool | None = None
    attempt_no: int = 1
    duration_ms: float | None = None
    outcome: str | None = None


class _AuditValidationError(ValueError):
    """Raised when an event contradicts its policy. Never reaches the caller."""


def _validate(record: ToolCallAuditRecord) -> None:
    """Enforce the event's own invariants against the record being built."""
    policy = EVENT_POLICIES[record.event]

    if record.outcome != policy.outcome:
        raise _AuditValidationError(
            f"{record.event}: outcome is canonical ({policy.outcome}), got {record.outcome}"
        )

    if not policy.dispatched_applicable:
        if record.dispatched is not None:
            raise _AuditValidationError(f"{record.event}: dispatched is not applicable")
    elif record.dispatched is None:
        raise _AuditValidationError(f"{record.event}: dispatched must be stated explicitly")
    elif policy.dispatch_must_have_occurred and not record.dispatched:
        raise _AuditValidationError(f"{record.event}: unreachable without a dispatch")

    if policy.requires_reason and record.reason_code is None:
        raise _AuditValidationError(f"{record.event}: a stable reason_code is required")


class Auditor:
    """Single emission point for audit records."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger if logger is not None else get_audit_logger()
        # Only when unconfigured: otherwise the INFO milestones drop at WARNING.
        if self._logger.level == logging.NOTSET:
            self._logger.setLevel(AUDIT_LOGGER_LEVEL)

    def emit(
        self,
        event: AuditEvent,
        *,
        tool_call_id: str,
        tool_name: str | None = None,
        tool_side_effects: ToolSideEffects | None = None,
        tool_version: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        investigation_id: str | None = None,
        job_id: str | None = None,
        authorization_decision: DecisionEffect | None = None,
        policy_id: str | None = None,
        policy_version: str | None = None,
        reason_code: ToolFailureReason | None = None,
        error_category: str | None = None,
        dispatched: bool | None = None,
        attempt_no: int = 1,
        duration_ms: float | None = None,
    ) -> AuditEmitStatus:
        """Build, validate and deliver one record; returns the delivery status.

        Never propagates ``Exception`` — a failure becomes FAILED so the
        surrounding tool call behaves as if auditing were off.
        """
        try:
            policy = EVENT_POLICIES[event]

            record = ToolCallAuditRecord(
                event=event,
                audit_event_id=str(uuid7()),
                occurred_at=datetime.now(timezone.utc),
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                tool_side_effects=tool_side_effects,
                tool_version=tool_version,
                actor_type=actor_type,
                actor_id=actor_id,
                authorization_decision=authorization_decision,
                policy_id=policy_id,
                policy_version=policy_version,
                reason_code=reason_code,
                error_category=error_category,
                dispatched=dispatched,
                attempt_no=attempt_no,
                duration_ms=duration_ms,
                outcome=policy.outcome,
            )
            _validate(record)

            levelno = logging.getLevelNamesMapping()[policy.severity]
            if not self._logger.isEnabledFor(levelno):
                return AuditEmitStatus.SKIPPED

            payload = self._to_payload(record)

            # Bound, not passed via extra: the record factory owns these names.
            with bind_correlation(investigation_id=investigation_id, job_id=job_id):
                self._logger.log(levelno, record.event.value, extra=payload)

            return self._delivery_status()

        except Exception:
            # Silent by design: auditing must not decide whether a tool call succeeds.
            return AuditEmitStatus.FAILED

    def _to_payload(self, record: ToolCallAuditRecord) -> dict[str, Any]:
        # Not redundant with the formatter: this is the only pass on the emit thread.
        return {k: sanitize(v) for k, v in record.model_dump(mode="json", exclude_none=True).items()}

    def _delivery_status(self) -> AuditEmitStatus:
        """Whether the record was queued for the listener or handled inline."""
        root = logging.getLogger()
        queued = any(h.name == QUEUE_HANDLER_NAME for h in root.handlers)
        return AuditEmitStatus.ENQUEUED if queued else AuditEmitStatus.EMBEDDED


#: Process-wide auditor. Stateless, so a single instance is safe to share.
auditor = Auditor()