from pydantic import BaseModel
from vouch.ledger.models import Evidence, LedgerItemSnapshot

class AppendResult(BaseModel):
    evidence_ids: list[str]
    ledger_seq_high: int

class EvidenceLedger:
    def __init__(self):
        ...

    def get_by_id(self, evidence_id: str) -> LedgerItemSnapshot:
        ...

    def append_many(self, evidence: list[Evidence]) -> AppendResult:
        ...
