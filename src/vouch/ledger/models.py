from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from vouch.agents.models import SourceRef
from vouch.security import EntityRef, Provenance, TrustLevel

class EvidenceDraft(BaseModel):
    """Application-validated evidence, awaiting ledger commit."""
    investigation_id: str
    kind: Literal["source_observation", "interpretation"]
    finding: str = Field(min_length=1, max_length=1200)

    source_refs: list[SourceRef]
    entity_refs: list[EntityRef] = Field(default_factory=list)

    trust: TrustLevel
    provenance: Provenance

# --- Ledger records ---

class Evidence(EvidenceDraft):
    """Append-only. Minted only by the ledger writer."""
    evidence_id: str
    ledger_seq: int
    investigation_id: str
    supersedes: str | None = None # evidence_id this replaces

class LedgerItemSnapshot(BaseModel):
    """What agents read. Immutable; a new ledger write means a new snapshot, never a mutation."""
    snapshot_id: str
    investigation_id: str
    ledger_seq_high: int
    evidence_ids: list[str]
    entities: list[EntityRef]
    created_at: datetime
    policy_version: str
