"""RDAP — domain registration age and registrar lookups."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from pydantic import BaseModel
from langchain.tools import tool

from vouch.preprocessing.models import EntityType
from vouch.security import EntityRef
from vouch.tools.facts._shared import (
    FactSource, SourceFact, ThreatClassification, call, host_of,
)

_YOUNG_DAYS = 30
_ESTABLISHED_DAYS = 730   # 2 years


class RdapArgs(BaseModel):
    """Args: the original URL string; the bare domain is derived internally."""
    url: str

    @classmethod
    def from_entity(cls, entity: EntityRef) -> RdapArgs:
        return cls(url=entity.value)


def _registrar(data: dict) -> str:
    """Pull the registrar fn out of the RDAP vCard array, if present."""
    for ent in data.get("entities") or []:
        vcard = ent.get("vcardArray") or []
        # vcardArray == ["version", [["fn", {}, "text", "Registrar Name"], ...]]
        if len(vcard) < 2:
            continue
        for item in vcard[1]:
            if isinstance(item, list) and len(item) >= 4 and item[0] == "fn":
                if isinstance(item[3], str) and item[3]:
                    return item[3]
    return ""


def _rdap_dt(raw: str) -> datetime | None:
    """Parse an RDAP event timestamp, forcing UTC when the value is offset-naive so the
    subtraction below can't raise."""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

@tool
async def lookup_rdap(args: RdapArgs) -> list[SourceFact]:
    now = datetime.now(timezone.utc)
    domain = host_of(args.url)
    if not domain:
        return []

    try:
        resp = await call(
            "rdap.org", "GET", f"https://rdap.org/domain/{quote(domain)}", timeout=15.0,
        )
    except Exception:
        return []
    if resp.status_code != 200:      # 404 = domain not registered — no age signal
        return []

    data = resp.json()
    events = {
        e.get("eventAction"): e.get("eventDate")
        for e in (data.get("events") or [])
    }
    registered_at = _rdap_dt(events.get("registration") or "")
    if registered_at is None:
        return []

    age_days = (now - registered_at).days
    if age_days < _YOUNG_DAYS:
        classification, confidence = ThreatClassification.DOMAIN_YOUNG, 0.8
    elif age_days >= _ESTABLISHED_DAYS:
        classification, confidence = ThreatClassification.DOMAIN_ESTABLISHED, None
    else:
        return []  # mid-age: not a notable signal either way

    return [SourceFact(
        source=FactSource.RDAP,
        indicator=EntityRef(type=EntityType.URL, value=args.url),
        classification=classification,
        observed_at=now,
        source_confidence=confidence,
        expires_at=now + timedelta(hours=24),
        detail={
            "domain": domain,
            "registered_at": registered_at.isoformat(),
            "age_days": age_days,
            "registrar": _registrar(data),
        },
    )]
