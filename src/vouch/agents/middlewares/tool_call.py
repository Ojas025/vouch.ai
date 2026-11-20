from collections.abc import Awaitable
from typing import Any, Callable
import json
from uuid import UUID

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from vouch.logger import get_logger
from vouch.tools.gateway import ToolGateway
from vouch.tools.models import (
    ToolCallRequest as GatewayRequest,
)
from vouch.utils import uuid7

logger = get_logger(__name__)

class ToolCallMiddleware(AgentMiddleware[Any, Any]):
    def __init__(self, gateway: ToolGateway):
        self.gateway = gateway

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]]) -> ToolMessage | Command[Any]:
        # Translate langchain request into GatewayRequest
        context = request.runtime.context
        gateway_request = GatewayRequest(
            tool_name = request.tool_call["name"],
            arguments = request.tool_call["args"],
            agent_role = context.agent_role.value if context else "unknown",
            task_id = str(context.task_id) if context and context.task_id is not None else "unknown",
        )

        # Invoke the tool via the gateway
        response = await self.gateway.invoke(str(context.investigation_id), request.tool_call["id"], gateway_request)

        if response.is_error:
            error = (
                response.failure_reason.value
                if response.failure_reason is not None
                else "UNKNOWN_ERROR"
            )

            payload: dict[str, Any] = {
                "ok": False,
                "error": error,
            }

        else:
            payload = {
                "ok": True,
                "result": response.result.model_dump(mode="json"),
            }

        return ToolMessage(
            content=json.dumps(payload),
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            status="error" if response.is_error else "success",
        )
