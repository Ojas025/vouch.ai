"""Security audit events for tool calls.

Audit records answer "who tried to do what, was it allowed, and what actually
happened". They are distinct from operational logs: an audit record is
attributable, security-relevant, and travels on its own logger so it can be
routed and retained separately.

Design constraints this module exists to enforce:

* One emission path. ``Auditor.emit`` is the only way an audit record is
  produced, so construction, correlation enrichment, validation, sanitisation
  and delivery cannot drift apart between call sites.
* Callers cannot restate canonical facts. Event name, severity and outcome come
  from :data:`EVENT_POLICIES`, and ``dispatched`` is derived from the actual
  execution state rather than trusted from the caller.
* Emission never raises into the caller. A tool call must not behave
  differently because an audit write failed, so every failure is swallowed and
  reported as a status instead.

Nothing here is tamper-proof, append-only or non-repudiable. Delivery stops at
"handed to the logging pipeline"; see :class:`AuditEmitStatus`.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from vouch.logger import AUDIT_LOGGER_LEVEL, QUEUE_HANDLER_NAME, get_audit_logger
from vouch.observability.context import bind_correlation
from vouch.observability.records import RESERVED_FACTORY_FIELDS
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

    Named to match what the implementation guarantees. ``ENQUEUED`` means the
    record was accepted by the logging API and put on the listener's queue --
    it is not written, transmitted or persisted. ``FAILED`` means the record was
    constructed and validated but delivery raised.
    """

    ENQUEUED = "enqueued"
    EMBEDDED = "embedded"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class AuditEventPolicy:
    """Canonical semantics for one event. Callers never supply these."""

    severity: str
    outcome: str | None
    #: Whether "did dispatch actually happen" is meaningful for this event.
    dispatched_applicable: bool
    #: Events unreachable unless the tool handler really was invoked.
    #: ``tool_call.failed`` deliberately does not set this: a call can fail at
    #: resolution, validation, authorization or rate limiting without ever
    #: reaching the handler, and that difference is the whole point of the field.
    dispatch_must_have_occurred: bool
    #: Events that cannot be interpreted without a stable reason code.
    requires_reason: bool


_EVENT_POLICY = AuditEventPolicy("INFO", None, False, False, False)
_DISPATCHED = AuditEventPolicy("INFO", None, True, True, False)
_DISPATCHED_SUCCESS = AuditEventPolicy("INFO", "success", True, True, False)
#: Failed covers both "never dispatched" and "dispatched and blew up".
_FAILED = AuditEventPolicy("WARNING", "failure", True, False, True)

#: Single source of truth for event severity, outcome and required fields.
EVENT_POLICIES: dict[AuditEvent, AuditEventPolicy] = {
    AuditEvent.TOOL_CALL_REQUESTED: _EVENT_POLICY,
    AuditEvent.TOOL_CALL_AUTHORIZATION_DECIDED: AuditEventPolicy("INFO", None, False, False, False),
    AuditEvent.TOOL_CALL_EXECUTION_STARTED: _DISPATCHED,
    AuditEvent.TOOL_CALL_COMPLETED: _DISPATCHED_SUCCESS,
    AuditEvent.TOOL_CALL_FAILED: _FAILED,
    AuditEvent.TOOL_CALL_TIMED_OUT: AuditEventPolicy("WARNING", "timeout", True, True, True),
    # No reason required: cancellation is not a ToolFailureReason, and
    # labelling it tool_error would misreport it to whoever reads the trail.
    AuditEvent.TOOL_CALL_CANCELLED: AuditEventPolicy("WARNING", "cancelled", True, True, False),
}


class ToolCallAuditRecord(BaseModel):
    """The payload of one tool-call audit event.

    ``extra="forbid"`` so a caller cannot smuggle an unvetted field into an
    audit record through the model either.
    """

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


def _validate(record: ToolCallAuditRecord) -> ToolCallAuditRecord:
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

    return record


class Auditor:
    """Single emission point for audit records."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger if logger is not None else get_audit_logger()
        # Applied only when nothing has configured this logger. Without it, a
        # process that never ran configure_logging() inherits the root level,
        # and at WARNING the INFO milestones are skipped while the WARNING
        # failures survive, leaving audit records with no lifecycle around them.
        # An explicit operator level still wins.
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
        """Build, validate and deliver one audit record.

        Returns the delivery status. Does not propagate ``Exception``: a failure
        anywhere in here becomes :attr:`AuditEmitStatus.FAILED` so the
        surrounding tool call behaves as if auditing were switched off.
        ``BaseException`` deliberately is not swallowed, so a cancellation
        racing this call still propagates.
        """
        try:
            policy = EVENT_POLICIES[event]

            record = _validate(
                ToolCallAuditRecord(
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
            )

            levelno = logging.getLevelNamesMapping()[policy.severity]
            if not self._logger.isEnabledFor(levelno):
                return AuditEmitStatus.SKIPPED

            payload = self._to_payload(record)

            # Correlation is bound rather than passed through ``extra``: the
            # record factory writes these names, and makeRecord raises
            # KeyError if a caller supplies them a second time.
            with bind_correlation(investigation_id=investigation_id, job_id=job_id):
                self._logger.log(levelno, record.event.value, extra=payload)

            return self._delivery_status()

        except Exception:
            # Deliberately broad, and deliberately silent: auditing must not be
            # able to change whether a tool call succeeds. The status is the
            # only signal a caller gets.
            return AuditEmitStatus.FAILED

    def _to_payload(self, record: ToolCallAuditRecord) -> dict[str, Any]:
        # ``event`` is kept: it is part of the canonical log schema, and the
        # repo's existing call sites already pass it that way.
        fields = record.model_dump(mode="json", exclude_none=True)
        collision = RESERVED_FACTORY_FIELDS.intersection(fields)
        if collision:
            raise _AuditValidationError(f"audit payload collides with reserved fields: {collision}")
        return {key: sanitize(value) for key, value in fields.items()}

    def _delivery_status(self) -> AuditEmitStatus:
        """Whether the record was queued for the listener or handled inline."""
        root = logging.getLogger()
        queued = any(h.name == QUEUE_HANDLER_NAME for h in root.handlers)
        return AuditEmitStatus.ENQUEUED if queued else AuditEmitStatus.EMBEDDED


#: Process-wide auditor. Stateless, so a single instance is safe to share.
auditor = Auditor()