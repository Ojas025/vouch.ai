import asyncio
import time

from pydantic import ValidationError
from vouch.logger import get_logger
from vouch.observability.audit import AuditEvent, auditor
from vouch.observability.spans import (
    ATTR_ATTEMPT_NO,
    ATTR_TOOL_CALL_ID,
    ATTR_TOOL_NAME,
    ATTR_TOOL_VERSION,
    SPAN_TOOL_EXECUTE,
    span,
)
from vouch.tools import policy
from vouch.tools.models import AgentContext, DecisionEffect, ToolCallRequest, ToolCallResponse, ToolFailureReason
from vouch.tools.rate_limit import RateLimiter
from vouch.tools.registry import ToolRegistry
from vouch.utils import uuid7

logger = get_logger(__name__)

class ToolGateway:
    def __init__(self, registry: ToolRegistry, rate_limiter: RateLimiter, max_output_chars: int = 10000):
        self.max_output_chars = max_output_chars
        self.registry = registry
        self.rate_limiter = rate_limiter

    async def invoke(self, context: AgentContext, req: ToolCallRequest) -> ToolCallResponse:
        tool_invocation_id = str(uuid7())

        def _failed_response(reason: ToolFailureReason, elapsed_ms: float = 0.0):
            return ToolCallResponse(is_error=True, failure_reason=reason, tool_invocation_id=tool_invocation_id, elapsed_ms=elapsed_ms)

        # Audit invocation.
        # ``_emit`` swallows everything: it is called from inside the
        # except-handlers below, where a raise would replace the tool result or
        # the pending cancellation, and from the success path after the finally
        # that binds elapsed_ms. Nothing here may influence the return value.
        def _emit(event: AuditEvent, **fields):
            try:
                auditor.emit(
                    event,
                    tool_call_id=tool_invocation_id,
                    tool_name=req.tool_name,
                    actor_type="AGENT",
                    actor_id=context.agent_id,
                    **fields,
                )
            except Exception:
                pass

        _emit(AuditEvent.TOOL_CALL_REQUESTED, attempt_no=1)

        # Resolve tool from registry
        tool = self.registry.resolve(req.tool_name)
        if not tool:
            _emit(AuditEvent.TOOL_CALL_FAILED, dispatched=False, reason_code=ToolFailureReason.TOOL_NOT_FOUND, error_category="tool_not_found")
            return _failed_response(ToolFailureReason.TOOL_NOT_FOUND)

        try:
            # Validate tool arguments
            validated_args = tool.args_model.model_validate(req.arguments)
        except ValidationError as e:
            _emit(AuditEvent.TOOL_CALL_FAILED, dispatched=False, reason_code=ToolFailureReason.INVALID_ARGUMENTS, error_category=type(e).__name__)
            return _failed_response(ToolFailureReason.INVALID_ARGUMENTS)

        # Check tool use permissions
        decision = policy.authorize(context, req.tool_name)

        # Emitted strictly after the decision, never between here and the
        # branch below: an audit write must not be able to influence it.
        _emit(
            AuditEvent.TOOL_CALL_AUTHORIZATION_DECIDED,
            tool_side_effects=tool.side_effects,
            tool_version=tool.version,
            authorization_decision=decision.effect,
            policy_id=policy.POLICY_ID,
            policy_version=policy.POLICY_VERSION,
        )

        if decision.effect == DecisionEffect.DENY:
            denied_reason = decision.reason or ToolFailureReason.UNAUTHORIZED_TOOL_ACCESS
            _emit(AuditEvent.TOOL_CALL_FAILED, dispatched=False, reason_code=denied_reason, error_category=decision.effect.value)
            if decision.reason is not None:
                return _failed_response(decision.reason)
            return _failed_response(ToolFailureReason.UNAUTHORIZED_TOOL_ACCESS)

        # Check rate limits
        if not self.rate_limiter.allow():
            _emit(AuditEvent.TOOL_CALL_FAILED, dispatched=False, reason_code=ToolFailureReason.RATE_LIMIT_REACHED, error_category="rate_limiter")
            return _failed_response(ToolFailureReason.RATE_LIMIT_REACHED)

        # Check investigation budget

        # Last point at which the caller is told dispatch is happening, and the
        # first at which dispatched=True is truthful.
        _emit(AuditEvent.TOOL_CALL_EXECUTION_STARTED, tool_side_effects=tool.side_effects, tool_version=tool.version, dispatched=True)

        logger.debug(
            "dispatching tool call",
            extra={
                "event": "tool_call.dispatch",
                "component": "tools.gateway",
                "tool_call_id": tool_invocation_id,
                "tool_name": req.tool_name,
            },
        )

        # Started after the audit work above so elapsed_ms, which the caller
        # receives, stays a measurement of the tool rather than of the
        # instrumentation.
        started = time.monotonic()

        try:
            # Execute tool handler
            with span(
                SPAN_TOOL_EXECUTE,
                {
                    ATTR_TOOL_NAME: req.tool_name,
                    ATTR_TOOL_VERSION: tool.version,
                    ATTR_TOOL_CALL_ID: tool_invocation_id,
                    ATTR_ATTEMPT_NO: 1,
                },
            ):
                # Re-baselined inside the span so elapsed_ms, which the caller
                # receives, measures the tool call and not the instrumentation
                # wrapped around it. `started` is already bound above, so this
                # stays safe if entering the span ever raises.
                started = time.monotonic()
                response = await asyncio.wait_for(self.registry.invoke(req.tool_name, validated_args), tool.timeout_sec)
        except asyncio.CancelledError:
            _emit(AuditEvent.TOOL_CALL_CANCELLED, dispatched=True, error_category="cancelled", duration_ms=(time.monotonic() - started) * 1000)
            raise
        except TimeoutError as e:
            elapsed = (time.monotonic() - started) * 1000
            _emit(AuditEvent.TOOL_CALL_TIMED_OUT, dispatched=True, reason_code=ToolFailureReason.TOOL_TIMEOUT, error_category=type(e).__name__, duration_ms=elapsed)
            logger.warning("tool call timed out", extra={"event": "tool_call.timeout", "component": "tools.gateway", "tool_call_id": tool_invocation_id, "tool_name": req.tool_name, "duration_ms": elapsed, "reason_code": ToolFailureReason.TOOL_TIMEOUT.value})
            return _failed_response(ToolFailureReason.TOOL_TIMEOUT, elapsed_ms=elapsed)
        except Exception as e:
            elapsed = (time.monotonic() - started) * 1000
            _emit(AuditEvent.TOOL_CALL_FAILED, dispatched=True, reason_code=ToolFailureReason.TOOL_ERROR, error_category=type(e).__name__, duration_ms=elapsed)
            logger.error("tool handler raised", extra={"event": "tool_call.error", "component": "tools.gateway", "tool_call_id": tool_invocation_id, "tool_name": req.tool_name, "duration_ms": elapsed, "error_category": type(e).__name__}, exc_info=True)
            return _failed_response(ToolFailureReason.TOOL_ERROR, elapsed_ms=elapsed)
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000

        try:
            # Valid response
            validated = tool.output_model.model_validate(response)
        except ValidationError as e:
            _emit(AuditEvent.TOOL_CALL_FAILED, dispatched=True, reason_code=ToolFailureReason.OUTPUT_VALIDATION_FAILED, error_category=type(e).__name__, duration_ms=elapsed_ms)
            return _failed_response(ToolFailureReason.OUTPUT_VALIDATION_FAILED)

        # Record output and settle budget

        _emit(AuditEvent.TOOL_CALL_COMPLETED, dispatched=True, tool_side_effects=tool.side_effects, tool_version=tool.version, duration_ms=elapsed_ms)

        # Return ToolCallResponse
        return ToolCallResponse(result=validated, tool_invocation_id=tool_invocation_id, elapsed_ms=elapsed_ms)