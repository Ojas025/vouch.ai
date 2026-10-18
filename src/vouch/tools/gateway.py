import asyncio
import time

from pydantic import ValidationError
from vouch.tools import policy
from vouch.tools.models import AgentContext, DecisionEffect, ToolCallRequest, ToolCallResponse, ToolFailureReason
from vouch.tools.rate_limit import RateLimiter
from vouch.tools.registry import ToolRegistry
from vouch.utils import uuid7

class ToolGateway:
    def __init__(self, registry: ToolRegistry, rate_limiter: RateLimiter, max_output_chars: int = 10000):
        self.max_output_chars = max_output_chars
        self.registry = registry
        self.rate_limiter = rate_limiter

    async def invoke(self, context: AgentContext, req: ToolCallRequest) -> ToolCallResponse:
        tool_invocation_id = str(uuid7())

        def _failed_response(reason: ToolFailureReason, elapsed_ms: float = 0.0):
            return ToolCallResponse(is_error=True, failure_reason=reason, tool_invocation_id=tool_invocation_id, elapsed_ms=elapsed_ms)

        # Resolve tool from registry
        tool = self.registry.resolve(req.tool_name)
        if not tool:
            return _failed_response(ToolFailureReason.TOOL_NOT_FOUND)

        try:
            # Validate tool arguments
            validated_args = tool.args_model.model_validate(req.arguments)
        except ValidationError:
            return _failed_response(ToolFailureReason.INVALID_ARGUMENTS)

        # Check tool use permissions
        decision = policy.authorize(context, req.tool_name)
        if decision.effect == DecisionEffect.DENY:
            if decision.reason is not None:
                return _failed_response(decision.reason)
            return _failed_response(ToolFailureReason.UNAUTHORIZED_TOOL_ACCESS)

        # Check rate limits
        if not self.rate_limiter.allow():
            return _failed_response(ToolFailureReason.RATE_LIMIT_REACHED)

        # Check investigation budget

        # Audit invocation

        started = time.monotonic()
        try:
            # Execute tool handler
            response = await asyncio.wait_for(self.registry.invoke(req.tool_name, validated_args), tool.timeout_sec)
        except TimeoutError:
            return _failed_response(ToolFailureReason.TOOL_TIMEOUT, elapsed_ms=(time.monotonic() - started) * 1000)
        except Exception:
            return _failed_response(ToolFailureReason.TOOL_ERROR, elapsed_ms=(time.monotonic() - started) * 1000)
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000

        try:
            # Valid response
            validated = tool.output_model.model_validate(response)
        except ValidationError:
            return _failed_response(ToolFailureReason.OUTPUT_VALIDATION_FAILED)

        # Record output and settle budget

        # Return ToolCallResponse
        return ToolCallResponse(result=validated, tool_invocation_id=tool_invocation_id, elapsed_ms=elapsed_ms)
