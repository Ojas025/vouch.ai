"""
# Tool Use

request = ToolRequest(
    agent_id=agent.id,
    tool=call.name,
    arguments=call.args,
    run_id=run.id
)

decision = security.authorize(request)

if not decision.allowed:
    return ToolDenied(decision.reason)

validated = security.validate_arguments(request, decision)

if decision.requires_approval:
    return approval_service.pause(validated)

result = tool_executor.execute(validated)
result = security.validate_output(result)

audit.log(request, decision, result)
"""

"""
# Prompt Injection

Carry control metadata at sensitive boundaries

ContextItem(
    content=page.text,
    trust=TrustLevel.RETRIEVED_CONTENT,
)
"""

"""
# Add audit logging at the security boundary

{
  "event": "tool_execution",
  "run_id": "r123",
  "agent_id": "researcher",
  "tool": "write_file",
  "arguments_hash": "abc...",
  "policy": "allow",
  "approval": null,
  "execution": "success",
  "policy_version": "v12"
}
"""

def validate_signature(raw_payload: dict, headers: dict) -> bool:
        ...
