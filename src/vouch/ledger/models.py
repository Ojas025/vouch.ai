from datetime import datetime

from pydantic import BaseModel

from src.vouch.branches.models import EvidencePayload
from src.vouch.security.models import EntityRef, Provenance, TrustLevel


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
