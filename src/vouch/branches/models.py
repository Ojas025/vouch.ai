from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from vouch.preprocessing.models import ExtractedEntity, Modality
from vouch.security.models import EntityRef, Provenance, TrustLevel


class ArtifactRef(BaseModel):
    sha256: str
    size_bytes: int
    sniffed_mime: str
    blob_ref: str

class BranchJob(BaseModel):
    job_id: str
    investigation_id: str
    modality: Modality
    artifact: ArtifactRef | None = None
    url: str | None = None
    sandbox_profile: str # names a CPU/mem/wall/net limit set
    attempt_no: int = 1

class EvidenceKind(str, Enum):
    SOURCE_FACT = "source_fact"
    OBSERVATION = "observation"
    SIMILARITY_MATCH = "similarity_match"

class SourceFactPayload(BaseModel):
    kind: Literal[EvidenceKind.SOURCE_FACT] = EvidenceKind.SOURCE_FACT
    source: Literal["URLHAUS", "THREATFOX", "PHISHTANK", "OPENPHISH", "GSB", "RDAP", "IFSC", "ALLOWLIST"]
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
    other_entity: EntityRef
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
    subject: EntityRef | None
    payload: EvidencePayload
    new_entities: list[ExtractedEntity] = Field(default_factory=list)

    trust: TrustLevel
    provenance: Provenance
