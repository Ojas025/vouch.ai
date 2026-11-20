from datetime import datetime
from enum import Enum
import operator
from typing import Annotated, Literal, TypedDict
from uuid import UUID, uuid4

from litellm.litellm_core_utils.streaming_chunk_builder_utils import concatenate_base64_list
from pydantic import BaseModel, ConfigDict, Field

from vouch.agents.llm import Capability, LLMProfile
from vouch.decision import DecisionRecord, Verdict
from vouch.ledger.models import EvidenceDraft, SourceRef
from vouch.preprocessing.models import EntityType
from vouch.security import EntityRef, Provenance, TrustLevel
from vouch.tools.models import ToolCallRequest

class ClaimType(str, Enum):
    FACTUAL = "factual"
    INFERENTIAL = "inferential"

class Hypotheses(BaseModel):
    hypotheses_id: str
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

class VerifierAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses_id: str
    status: Literal["valid", "invalid", "inconclusive"]
    supporting_evidence_refs: list[str] = Field(default_factory=list)
    contradicting_evidence_refs: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    provenance: Provenance
    trust: TrustLevel = TrustLevel.MODEL_OUTPUT

class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    HYPOTHESIS_AGENT = "hypotheses_agent"
    VERIFIER = "verifier"
    SYNTHESIS = "synthesis"
    APK_AGENT = "apk_agent"
    WEB_AGENT = "web_agent"
    ARTIFACT_AGENT = "artifact_agent"

class AgentProfile(BaseModel):
    role: AgentRole
    llm_profile: LLMProfile
    allowed_tools: frozenset[str]
    network_policy: Literal["none", "search_only"]
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
    claims: list[Hypotheses] = Field(default_factory=list)
    tool_requests: list[ToolCallRequest] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: datetime

class TaskLimits(BaseModel):
    max_iterations: int = 5
    max_model_calls: int = 10
    max_tool_calls: int = 20
    max_runtime_s: int = 60
    max_cost_usd: float = 0.0

class Status(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"

class Task(BaseModel):
    task_id: UUID = Field(default_factory=uuid4)
    assigned_to: AgentRole
    scoped_refs: set[str] = Field(
        default_factory=set,
        description="A list of IDs from the ledger that the task is scoped out of",
    )
    objective: str
    status: Status = Status.PENDING
    limits: TaskLimits | None = None

class EntityCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: EntityType

    # The value as observed in the source, before canonicalization.
    observed_value: str = Field(min_length=1, max_length=2048)

    source_refs: list[SourceRef] = Field(min_length=1, max_length=4)

class EvidenceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding: str = Field(min_length=1, max_length=1200, description="One concise, atomic finding grounded in the cited sources.")
    kind: Literal["source_observation", "interpretation"]
    source_refs: list[SourceRef] = Field(min_length=1, max_length=4)
    entity_candidates: list[EntityCandidate] = Field(default_factory=list)

class EvidenceCandidateBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[EvidenceCandidate] = Field(default_factory=list)

class WorkerResult(BaseModel):
    task_id: UUID
    agent: AgentRole
    status: Status
    candidate_count: int = 0
    errors: list[str] = Field(default_factory=list)

class Synthesis(BaseModel):
    decision: DecisionRecord
    response: str

class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: UUID
    tasks: list[Task] = Field(default_factory=list)
    disposition: Literal["dispatch", "no_actionable_tasks", "blocked", "malformed_tasks"]

class InvestigationState(TypedDict):
    investigation_id: UUID
    original_request: str

    task_plan: TaskPlan | None
    worker_results: Annotated[list[WorkerResult], operator.add]

    drafts: Annotated[list[EvidenceDraft], operator.add]
    accepted_evidences: Annotated[list[str], operator.add]
    hypotheses: Annotated[list[Hypotheses], operator.add]
    verifier_assessments: Annotated[list[VerifierAssessment], operator.add]

    verdict: Verdict | None
    synthesis: Synthesis | None

    exhausted: Literal["recursion", "budget", "timeout"] | None
    inconclusive: bool

    stage: Literal["orchestration", "modality", "hypotheses", "verification", "synthesis"]
    iterations: int

class TaskExecutionInput(TypedDict):
    investigation_id: UUID
    task: Task
    input_message: dict

class HypothesesOutput(BaseModel):
    hypotheses: list[Hypotheses] = Field(default_factory=list)

class OrchestratorOutput(BaseModel):
    disposition: Literal["dispatch", "no_actionable_tasks", "blocked"]
    tasks: list[Task] = Field(default_factory=list)

class VerifierOutput(BaseModel):
    verifier_assessments: list[VerifierAssessment] = Field(default_factory=list)
    verdict: Verdict | None = None

class SynthesisOutput(BaseModel):
    response: str
    decision: Literal["inconclusive", "risky", "safe"]
