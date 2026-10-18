from vouch.tools.models import AgentContext, AuthorizationDecision, DecisionEffect, ToolFailureReason, ToolSideEffects

POLICY_ID = "tool_permissions"
POLICY_VERSION = "1.0"

def authorize(context: AgentContext, tool_name: str) -> AuthorizationDecision:
    """Authorize tool call access based on agent permissions"""
    tool = None
    for t in context.allowed_tools:
        if t.tool_name == tool_name:
            tool = t
            break

    if not tool:
        # requires_approval=True for escalating decision if tool_call is required?
        return AuthorizationDecision(effect=DecisionEffect.DENY, reason=ToolFailureReason.UNAUTHORIZED_TOOL_ACCESS)

    # check network policy
    if tool.side_effects is ToolSideEffects.NETWORK and context.network_policy == "none":
        return AuthorizationDecision(effect=DecisionEffect.DENY, reason=ToolFailureReason.NETWORK_DISALLOWED)

    return AuthorizationDecision(effect=DecisionEffect.PERMIT, requires_approval=False)
