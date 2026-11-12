from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

class ThreatClass(str, Enum):
    PHISHING = "phishing"
    IMPERSONATION = "impersonation"
    CREDENTIAL_THEFT = "credential_theft"
    PAYMENT_FRAUD = "payment_fraud"
    MALWARE = "malware"
    SOCIAL_MANIPULATION = "social_manipulation"

class Contradiction(BaseModel):
    claim_a: str
    claim_b: str
    critical: bool
    kind: Literal["direct_negation", "source_disagreement", "stale_vs_fresh"]

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

class DecisionStatus(str, Enum):
    DECIDED = "decided"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ANALYSIS_UNAVAILABLE = "analysis_unavailable"

class DecisionRecord(BaseModel):
    decision_id: str
    investigation_id: str
    status: DecisionStatus
    verdicts: list[Verdict]
    resolved_hypotheses_ids: list[str]
    unresolved_hypotheses_ids: list[str]
    contradictions: list[Contradiction]
    evidence_confidence: Literal["low", "medium", "high"]
    evidence_freshness: Literal["stale", "mixed", "fresh"]
    decided_at: datetime
