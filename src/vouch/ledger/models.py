from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from vouch.ingestion import AttachmentMetadata
from vouch.preprocessing.models import ExtractedEntity, Modality
from vouch.security import EntityRef, Provenance, TrustLevel

# --- Evidence contract (moved from branches/models.py) ---

class EvidenceKind(str, Enum):
    SOURCE_FACT = "source_fact"
    OBSERVATION = "observation"
    SIMILARITY_MATCH = "similarity_match"

class SourceFactPayload(BaseModel):
    kind: Literal[EvidenceKind.SOURCE_FACT] = EvidenceKind.SOURCE_FACT
    source: Literal["URLHAUS", "THREATFOX", "OPENPHISH", "RDAP", "IFSC", "URLSCAN", "VIRUSTOTAL", "ALLOWLIST"]
    classification: str
    source_confidence: float | None = None
    detail: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

class ObservationPayload(BaseModel):
    """A typed, non-interpretive fact read from an artifact. Never an opinion."""
    kind: Literal[EvidenceKind.OBSERVATION] = EvidenceKind.OBSERVATION
    modality: Modality
    key: str
    value: str | int | float | bool | list[str]
    truncated: bool = False

class SimilarityMatchPayload(BaseModel):
    kind: Literal[EvidenceKind.SIMILARITY_MATCH] = EvidenceKind.SIMILARITY_MATCH
    method: Literal["minhash", "icon_phash", "cert_fingerprint", "entity_graph"]
    other_entity: AttachmentMetadata
    score: float
    verified: bool

EvidencePayload = Annotated[
    Union[SourceFactPayload, ObservationPayload, SimilarityMatchPayload],
    Field(discriminator="kind")
]

class EvidenceDraft(BaseModel):
    """What branches and fact services emit. Not yet in the ledger."""
    investigation_id: str
    status: Literal["ok", "partial", "timeout", "sandbox_violation", "parse_error"]
    modality: Modality
    subject: AttachmentMetadata | None
    payload: EvidencePayload
    new_entities: list[ExtractedEntity] = Field(default_factory=list)

    trust: TrustLevel
    provenance: Provenance

# --- Ledger records ---

class Evidence(BaseModel):
    """Append-only. Minted only by the ledger writer."""
    evidence_id: str
    ledger_seq: int
    investigation_id: str
    subject: EntityRef | None
    payload: EvidencePayload
    trust: TrustLevel
    provenance: Provenance
    supersedes: str | None = None # evidence_id this replaces

class InvestigationSnapshot(BaseModel):
    """What agents read. Immutable; a new ledger write means a new snapshot, never a mutation."""
    snapshot_id: str
    investigation_id: str
    ledger_seq_high: int
    evidence_ids: list[str]
    entities: list[EntityRef]
    created_at: datetime
    policy_version: str
