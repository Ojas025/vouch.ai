from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from vouch.security import EntityRef, Provenance, TrustLevel
from vouch.tools.models import ToolCallRequest


class ThreatClass(str, Enum):
    PHISHING = "phishing"
    IMPERSONATION = "impersonation"
    CREDENTIAL_THEFT = "credential_theft"
    PAYMENT_FRAUD = "payment_fraud"
    MALWARE = "malware"
    SOCIAL_MANIPULATION = "social_manipulation"

class ClaimType(str, Enum):
    FACTUAL = "factual"
    INFERENTIAL = "inferential"

class Claim(BaseModel):
    claim_id: str
    investigation_id: str
    snapshot_id: str # claim_id only valid for a particular snapshot_id
    subject: EntityRef | Literal["SUBMISSION"]
    object: str
    asserted: bool = True
    scope: str
    claim_type: ClaimType
    evidence_refs: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    observed_at: datetime
    expires_at: datetime | None = None
    provenance: Provenance
    trust: Literal[TrustLevel.MODEL_OUTPUT, TrustLevel.DETERMINISTIC] = TrustLevel.MODEL_OUTPUT

class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    ANALYST = "analyst"
    WEB_SEARCH = "web_search"
    SMD_CSD = "smd_csd"
    AUDITOR = "auditor"

class Agent(BaseModel):
    agent_id: str
    investigation_id: str
    run_id: str
    role: AgentRole
    allowed_tools: list[str]
    allowed_evidence_scopes: list[str]
    max_cost_usd: float
    max_runtime_s: int
    network_policy: Literal["none", "search_only"]
    model_id: str
    prompt_version: str

class MessageType(str, Enum):
    CLAIMS = "claims"
    TOOL_REQUEST = "tool_request"
    TOOL_RESULT = "tool_result"
    AUDIT_FINDING = "audit_finding"
    DONE = "done"

class AgentMessage(BaseModel):
    message_id: str
    investigation_id: str
    sender_agent_id: str
    receiver_agent_id: str
    message_type: MessageType
    claims: list[Claim] = Field(default_factory=list)
    tool_requests: list[ToolCallRequest] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: datetime

Capability = Literal[
    "vision",
    "json_schema",
    "tools",
    "streaming"
]

class LLMProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    alias: str = ""
    base_url: str = ""
    api_key: str | None = None

    capabilities: set[Capability] = Field(default_factory=set)
    timeout_sec: float = Field(default=60, gt=0)
    max_retries: int = Field(default=3, ge=0, le=5)
    model_kwargs: dict[str, Any] = Field(default_factory=dict)

    temperature: float = 1.0
    max_tokens: int = 1000
