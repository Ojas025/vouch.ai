from cmath import exp
import hashlib
import hmac

from typing import Literal
from enum import Enum
from pydantic import BaseModel
from datetime import datetime

from vouch.preprocessing.models import EntityType

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

def verify_whatsapp_signature(body: bytes, signature: str, app_secret: str) -> bool:
    if not signature:
        return False

    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()

    expected = f"sha256={expected}"

    return hmac.compare_digest(expected, signature)

def verify_telegram_token(body: bytes, token: str, secret_token: str) -> bool:
    if not token:
        return False

    return token == secret_token

# --- Trust & identity models (moved from security/models.py) ---

class EntityRef(BaseModel):
    """A normalized reference to a typed entity extracted from a submission."""
    type: EntityType
    value: str  # normalized_value from ExtractedEntity

class TrustLevel(str, Enum):
    SYSTEM = "system" # system metadata
    DETERMINISTIC = "deterministic" # deterministic code generated artifacts
    EXTERNAL_SOURCE = "external_source"
    UNTRUSTED_ARTIFACT = "untrusted_artifact" # anything read out of browser/apk/doc/image
    MODEL_OUTPUT = "model_output" # LLM-produced, may only exist as a Claim

class Provenance(BaseModel):
    """Who produced this, with what tool, from which bytes"""
    producer_type: Literal["AGENT", "SERVICE", "BRANCH"]
    producer_id: str
    tool_name: str
    tool_version: str
    prompt_version: str | None = None
    model_id: str | None = None
    sandbox_id: str | None = None
    artifact_sha256: str | None = None # which bytes were read as input
    observed_at: datetime
