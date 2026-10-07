from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from vouch.investigation.models import ThreatClass


class RiskLevel(int, Enum):
    NONE = 0
    WEAK = 1
    SUSPECTED = 2
    CONFIRMED = 3

class Verdict(BaseModel):
    threat_class: ThreatClass
    level: RiskLevel
    supporting_claim_ids: list[str]
    refuting_claim_ids: list[str] = Field(default_factory=list)
    rule_id: str

class Contradiction(BaseModel):
    claim_a: str
    claim_b: str
    critical: bool
    kind: Literal["direct_negation", "source_disagreement", "stale_vs_fresh"]

class EvidenceCoverage(BaseModel):
    required_scopes: list[str]
    resolved_scopes: list[str]
    unresolved_scopes: list[str]
    stale_evidence_ids: list[str] = Field(default_factory=list)

class DecisionStatus(str, Enum):
    DECIDED = "decided"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ANALYSIS_UNAVAILABLE = "analysis_unavailable"

class DecisionRecord(BaseModel):
    decision_id: str
    investigation_id: str
    snapshot_id: str
    status: DecisionStatus
    verdicts: list[Verdict]
    resolved_claim_ids: list[str]
    unresolved_claim_ids: list[str]
    contradictions: list[Contradiction]
    coverage: EvidenceCoverage
    evidence_confidence: Literal["low", "medium", "high"]
    evidence_freshness: Literal["stale", "mixed", "fresh"]
    auditor_invoked: bool
    policy_version: str
    scorer_version: str
    decided_at: datetime
