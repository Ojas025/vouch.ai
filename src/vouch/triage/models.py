from enum import Enum
from pydantic import BaseModel, Field

from vouch.preprocessing.models import Modality
from vouch.security.models import EntityRef

class TriageTier(int, Enum):
    EXACT = 0
    MINHASH = 1
    ENTITY_GRAPH = 2
    DENSE = 3
    MODALITY = 4
    LLM = 5

class TriageHit(BaseModel):
    tier: TriageTier
    matched_entity: EntityRef | None = None
    matched_investigation_id: str | None = None
    matched_evidence_ids: list[str] = Field(default_factory=list)
    similarity: float | None = None
    verified_exact: bool = False

class TriagePlan(BaseModel):
    submission_id: str
    investigation_id: str
    hits: list[TriageHit]
    branches: list[Modality]
    fact_lookups: list[EntityRef]
    revalidate_evidence_ids: list[str] = Field(default_factory=list)
