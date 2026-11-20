import asyncio
import time
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import func, update
from vouch.agents.models import AgentProfile, AgentRole
from vouch.agents.registry import PROFILES
from vouch.db.models import ToolCalls
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
from vouch.tools.exceptions import RetryableToolError
from vouch.tools.models import DecisionEffect, ToolCallRequest, ToolCallResponse, ToolFailureReason, ToolSideEffects
from vouch.tools.rate_limit import RateLimiter
from vouch.tools.registry import ToolRegistry

logger = get_logger(__name__)

class ToolAdmissionResponse(BaseModel):
    is_permitted: bool
    result: Any | None = None
    failure_reason: ToolFailureReason | None = None
    attempts: int
    status: str

class ToolGateway:
    def __init__(self, registry: ToolRegistry, rate_limiter: RateLimiter, session_factory: async_sessionmaker[AsyncSession], max_output_chars: int = 10000):
        self.max_output_chars = max_output_chars
        self.registry = registry
        self.rate_limiter = rate_limiter
        self.session_factory = session_factory

    async def _get_or_update_tool_call(self, investigation_id: str, tool_call_id: str, tool_name: str, tool_version: str, agent_role: str, task_id: str, args: dict) -> ToolAdmissionResponse | None:
        logger.info(
            "Registering tool call",
            extra={
                "event": "tool_call.register",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "tool_version": tool_version,
                "agent_role": agent_role,
                "task_id": task_id,
                "args": args,
            }
        )

        async with self.session_factory() as session:
            async with session.begin():
                try:
                    result = await session.execute(
                        insert(ToolCalls).values(
                            tool_call_id=tool_call_id,
                            tool_name=tool_name,
                            tool_version=tool_version,
                            agent_role=agent_role,
                            task_id=task_id,
                            args=args,
                            status="running",
                            attempts=1
                        ).on_conflict_do_update(
                            index_elements=[
                                "investigation_id",
                                "tool_call_id",
                                "task_id"
                            ],
                            set_={
                                "attempts": ToolCalls.attempts + 1,
                                "status": "running",
                                "updated_at": func.now()
                            },
                            where=(ToolCalls.status == "retryable_failed")
                        ).returning(ToolCalls.tool_call_id, ToolCalls.attempts, ToolCalls.status)
                    )

                    row = result.mappings().one_or_none()
                    if row is not None:
                        logger.info(f"Tool call registered successfully: {tool_call_id}. Attempt number: {row['attempts']}")
                        return ToolAdmissionResponse(is_permitted=True, attempts=row['attempts'], status=row['status'])

                    # Conflict existed, not eligible for another attempt
                    existing = await session.scalar(
                        select(ToolCalls).where(
                            ToolCalls.investigation_id == investigation_id,
                            ToolCalls.tool_call_id == tool_call_id,
                            ToolCalls.task_id == task_id
                        )
                    )

                    if existing is None:
                        raise RuntimeError("ToolCall record unexpectedly missing")

                    if existing.status == "completed":
                        return ToolAdmissionResponse(is_permitted=False, attempts=existing.attempts, status=existing.status, result=existing.result)

                    elif existing.status == "failed":
                        return ToolAdmissionResponse(is_permitted=False, attempts=existing.attempts, status=existing.status, failure_reason=ToolFailureReason(existing.failure_reason))

                except Exception as e:
                    logger.error(
                        "Failed to register tool call",
                        extra={
                            "event": "tool_call.register_failed",
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "tool_version": tool_version,
                            "agent_role": agent_role,
                            "task_id": task_id,
                            "args": args,
                        },
                        exc_info=True
                    )
                    raise e

    async def _update_tool_call_result(self, tool_call_id: str, elapsed_ms: float, result: Any = None, is_retryable: bool = False, failure_reason: ToolFailureReason | None = None):
        logger.info(
            "Updating tool call result",
            extra={
                "event": "tool_call.update_result",
                "tool_call_id": tool_call_id,
                "result": result,
                "failure_reason": failure_reason,
                "elapsed_ms": elapsed_ms,
            }
        )

        status = "completed" if failure_reason is None else ("retryable_failed" if is_retryable else "failed")

        async with self.session_factory() as session:
            async with session.begin():
                try:
                    tool_call = (
                        await session.execute(
                            update(ToolCalls).where(ToolCalls.tool_call_id == tool_call_id).values(
                                result=result,
                                failure_reason=failure_reason.value if failure_reason else None,
                                status=status,
                                elapsed_ms=elapsed_ms,
                                updated_at=func.now(),
                            ).returning(ToolCalls.tool_call_id, ToolCalls.status)
                        )
                    ).scalar_one_or_none()

                    if tool_call is None:
                        raise RuntimeError("ToolCall record not found for update")

                    logger.info(
                        "Tool call result updated successfully",
                        extra={
                            "event": "tool_call.update_result_success",
                            "tool_call_id": tool_call_id,
                            "status": status,
                            "elapsed_ms": elapsed_ms,
                        }
                    )

                except Exception as e:
                    logger.error(
                        "Failed to update tool call result",
                        extra={
                            "event": "tool_call.update_result_failed",
                            "tool_call_id": tool_call_id,
                            "result": result,
                            "failure_reason": failure_reason,
                            "elapsed_ms": elapsed_ms,
                        },
                        exc_info=True
                    )
                    raise e

    async def invoke(self, investigation_id: str, tool_call_id: str,  req: ToolCallRequest) -> ToolCallResponse:

        def _failed_response(reason: ToolFailureReason, elapsed_ms: float = 0.0):
            return ToolCallResponse(is_error=True, failure_reason=reason, tool_call_id=tool_call_id, elapsed_ms=elapsed_ms)

        # Swallows everything: it runs inside the except-handlers below.
        def _emit(event: AuditEvent, **fields):
            try:
                auditor.emit(
                    event,
                    tool_call_id=tool_call_id,
                    tool_name=req.tool_name,
                    actor_type="AGENT",
                    actor_id=req.agent_role,
                    **fields,
                )
            except Exception:
                pass

        _emit(AuditEvent.TOOL_CALL_REQUESTED, attempt_no=1)

        # Check Investigations if tool call was already made
        # If not, register tool call
        resp = await self._get_or_update_tool_call(investigation_id, tool_call_id, req.tool_name, "1.0", req.agent_role, req.task_id, args=req.arguments)
        if resp is not None and resp.is_permitted is False:
            if resp.status == "completed":
                return ToolCallResponse(result=resp.result, tool_call_id=tool_call_id, elapsed_ms=0.0)

            elif resp.status == "failed":
                if resp.failure_reason is not None:
                    return _failed_response(resp.failure_reason)

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

        agent_profile: AgentProfile = PROFILES[AgentRole(req.agent_role)]

        # Check tool use permissions
        decision = policy.authorize(agent_profile.allowed_tools, agent_profile.network_policy, req.tool_name)

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

            raise RetryableToolError("Rate limit reached")

        # Check investigation budget

        # First point at which dispatched=True is truthful.
        _emit(AuditEvent.TOOL_CALL_EXECUTION_STARTED, tool_side_effects=tool.side_effects, tool_version=tool.version, dispatched=True)

        logger.debug(
            "dispatching tool call",
            extra={
                "event": "tool_call.dispatch",
                "component": "tools.gateway",
                "tool_call_id": tool_call_id,
                "tool_name": req.tool_name,
            },
        )

        started = time.monotonic()

        try:
            # Execute tool handler
            with span(
                SPAN_TOOL_EXECUTE,
                {
                    ATTR_TOOL_NAME: req.tool_name,
                    ATTR_TOOL_VERSION: tool.version,
                    ATTR_TOOL_CALL_ID: tool_call_id,
                    ATTR_ATTEMPT_NO: resp.attempts,
                },
            ):
                # Re-baselined so elapsed_ms measures the tool, not the span.
                started = time.monotonic()

                response = await asyncio.wait_for(self.registry.invoke(req.tool_name, validated_args), tool.timeout_sec)

        except asyncio.CancelledError:
            _emit(AuditEvent.TOOL_CALL_CANCELLED, dispatched=True, error_category="cancelled", duration_ms=(time.monotonic() - started) * 1000)
            raise

        except TimeoutError as e:
            elapsed = (time.monotonic() - started) * 1000

            _emit(AuditEvent.TOOL_CALL_TIMED_OUT, dispatched=True, reason_code=ToolFailureReason.TOOL_TIMEOUT, error_category=type(e).__name__, duration_ms=elapsed)
            logger.warning("tool call timed out", extra={"event": "tool_call.timeout", "component": "tools.gateway", "tool_call_id": tool_call_id, "tool_name": req.tool_name, "duration_ms": elapsed, "reason_code": ToolFailureReason.TOOL_TIMEOUT.value})

            if tool.side_effects in (ToolSideEffects.WRITE, ToolSideEffects.DESTRUCTIVE):
                logger.error("tool call timed out for a write/destructive tool, not retrying", extra={"event": "tool_call.timeout", "component": "tools.gateway", "tool_call_id": tool_call_id, "tool_name": req.tool_name, "duration_ms": elapsed, "reason_code": ToolFailureReason.TOOL_TIMEOUT.value})
                return _failed_response(ToolFailureReason.TOOL_TIMEOUT, elapsed_ms=elapsed)

            raise RetryableToolError("Tool call timed out") from e

        except RetryableToolError as e:
            elapsed = (time.monotonic() - started) * 1000

            _emit(AuditEvent.TOOL_CALL_FAILED, dispatched=True, reason_code=ToolFailureReason.TOOL_ERROR, error_category=type(e).__name__, duration_ms=elapsed)
            logger.warning("tool call failed with retryable error", extra={"event": "tool_call.retryable_error", "component": "tools.gateway", "tool_call_id": tool_call_id, "tool_name": req.tool_name, "duration_ms": elapsed, "error_category": type(e).__name__}, exc_info=True)

            await self._update_tool_call_result(tool_call_id, elapsed_ms=elapsed, failure_reason=ToolFailureReason.TOOL_ERROR, is_retryable=True)
            raise

        except Exception as e:
            elapsed = (time.monotonic() - started) * 1000

            _emit(AuditEvent.TOOL_CALL_FAILED, dispatched=True, reason_code=ToolFailureReason.TOOL_ERROR, error_category=type(e).__name__, duration_ms=elapsed)
            logger.error("tool handler raised", extra={"event": "tool_call.error", "component": "tools.gateway", "tool_call_id": tool_call_id, "tool_name": req.tool_name, "duration_ms": elapsed, "error_category": type(e).__name__}, exc_info=True)

            await self._update_tool_call_result(tool_call_id, elapsed_ms=elapsed, failure_reason=ToolFailureReason.TOOL_ERROR, is_retryable=False)
            return _failed_response(ToolFailureReason.TOOL_ERROR, elapsed_ms=elapsed)

        finally:
            elapsed_ms = (time.monotonic() - started) * 1000

        try:
            # Valid response
            validated = tool.output_model.model_validate(response)

        except ValidationError as e:
            _emit(AuditEvent.TOOL_CALL_FAILED, dispatched=True, reason_code=ToolFailureReason.OUTPUT_VALIDATION_FAILED, error_category=type(e).__name__, duration_ms=elapsed_ms)

            await self._update_tool_call_result(tool_call_id, failure_reason=ToolFailureReason.OUTPUT_VALIDATION_FAILED, elapsed_ms=elapsed_ms)
            return _failed_response(ToolFailureReason.OUTPUT_VALIDATION_FAILED)

        # Record output and settle budget

        _emit(AuditEvent.TOOL_CALL_COMPLETED, dispatched=True, tool_side_effects=tool.side_effects, tool_version=tool.version, duration_ms=elapsed_ms)
        await self._update_tool_call_result(tool_call_id, result=validated, elapsed_ms=elapsed_ms)

        # Return ToolCallResponse
        return ToolCallResponse(result=validated, tool_call_id=tool_call_id, elapsed_ms=elapsed_ms)
