from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from vouch.security.models import EntityRef


class FactSource(str, Enum):
    URLHAUS     = "URLHAUS"
    THREATFOX   = "THREATFOX"
    OPENPHISH   = "OPENPHISH"
    RDAP        = "RDAP"
    IFSC        = "IFSC"
    URLSCAN     = "URLSCAN"
    VIRUSTOTAL  = "VIRUSTOTAL"


class ThreatClassification(str, Enum):
    MALWARE_DISTRIBUTION = "MALWARE_DISTRIBUTION"
    C2                   = "C2"
    PHISHING             = "PHISHING"
    DOMAIN_YOUNG         = "DOMAIN_YOUNG"       # RDAP: registered <30 days ago
    DOMAIN_ESTABLISHED   = "DOMAIN_ESTABLISHED" # RDAP: registered >2 years ago
    IFSC_VALID           = "IFSC_VALID"
    IFSC_INVALID         = "IFSC_INVALID"
    UNKNOWN              = "UNKNOWN"

    # urlscan / VirusTotal
    MALICIOUS_DOMAIN = "MALICIOUS_DOMAIN"   # positive verdict from urlscan or VT
    NO_SCAN_HISTORY  = "NO_SCAN_HISTORY"    # never scanned — weak, and NOT "verified clean"


class SourceFact(BaseModel):
    """A single intelligence record from a fact source. Immutable once created."""
    source: FactSource
    indicator: EntityRef
    classification: ThreatClassification
    observed_at: datetime
    source_confidence: float | None = Field(None, ge=0.0, le=1.0)
    expires_at: datetime | None = None
    detail: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at


class FactQuery(BaseModel):
    """What we ask fact services to look up."""
    entity: EntityRef
    investigation_id: str
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FactLookupStatus(str, Enum):
    HIT     = "hit"
    MISS    = "miss"
    CACHED  = "cached"
    ERROR   = "error"
    SKIPPED = "skipped"


class FactServiceResult(BaseModel):
    """The complete result of a lookup() call."""
    query: FactQuery
    status: FactLookupStatus
    facts: list[SourceFact] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)   # non-fatal source errors
    duration_ms: float = 0.0
    cache_hit: bool = False
