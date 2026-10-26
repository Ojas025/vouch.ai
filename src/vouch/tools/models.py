from enum import Enum
from typing import Any, Callable, Literal

from pydantic import BaseModel

class ToolSideEffects(str, Enum):
    READ_ONLY =  "read_only"
    NETWORK = "network"
    WRITE = "write"
    DESTRUCTIVE = "destructive"

class ToolDefinition(BaseModel):
    tool_name: str
    description: str
    handler: Callable
    args_model: type[BaseModel]
    output_model: type[BaseModel]
    version: str = "1.0"
    side_effects: ToolSideEffects = ToolSideEffects.READ_ONLY
    timeout_sec: float = 10.0

class ToolCallRequest(BaseModel):
    """
    - Agent emits ToolCallRequest
    - Request flow:
        Agent -> ToolCallRequest -> Proxy -> Tool layer
    """
    tool_name: str
    arguments: dict
    agent_id: str
    task_id: str

class ToolFailureReason(str, Enum):
    TOOL_NOT_FOUND = "tool_not_found"
    UNAUTHORIZED_TOOL_ACCESS = "unauthorized_tool_access"
    RATE_LIMIT_REACHED = "rate_limit_reached"
    INVALID_ARGUMENTS = "invalid_arguments"
    BUDGET_EXCEEDED = "budget_exceeded"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_ERROR = "tool_error"
    OUTPUT_VALIDATION_FAILED = "output_validation_failed"
    APPROVAL_REJECTED = "approval_rejected"
    NETWORK_DISALLOWED = "network_disallowed"

class DecisionEffect(str, Enum):
    PERMIT = "permit"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"

class AuthorizationDecision(BaseModel):
    effect: DecisionEffect
    reason: ToolFailureReason | None = None
    requires_approval: bool = False

class ToolCallResponse(BaseModel):
    is_error: bool = False
    failure_reason: ToolFailureReason | None = None
    result: Any = None
    elapsed_ms: float = 0.0
    tool_invocation_id: str

class AgentContext(BaseModel):
    agent_id: str
    allowed_tools: list[ToolDefinition]
    network_policy: Literal["none", "search_only"] = "none"
