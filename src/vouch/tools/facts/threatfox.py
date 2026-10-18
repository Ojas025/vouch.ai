"""ThreatFox (abuse.ch) — C2 / IOC malware lookups for an indicator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from langchain.tools import tool

from vouch.preprocessing.models import EntityType
from vouch.security import EntityRef
from vouch.tools.facts._shared import (
    _ABUSE_CH_HEADERS, FactSource, SourceFact, ThreatClassification, call,
)

_THREAT_CLASSES = {
    "payload_delivery": ThreatClassification.MALWARE_DISTRIBUTION,
    "c2": ThreatClassification.C2,
    "botnet_cc": ThreatClassification.C2,
}


class ThreatFoxArgs(BaseModel):
    """Args: the raw IOC string plus the entity type it was extracted as."""
    ioc: str
    entity_type: EntityType

    @classmethod
    def from_entity(cls, entity: EntityRef) -> ThreatFoxArgs:
        return cls(ioc=entity.value, entity_type=entity.type)

@tool
async def lookup_threatfox(args: ThreatFoxArgs) -> list[SourceFact]:
    now = datetime.now(timezone.utc)
    try:
        resp = await call(
            "abuse.ch", "POST", "https://threatfox-api.abuse.ch/api/v1/",
            json={"query": "search_ioc", "search_term": args.ioc},
            headers=_ABUSE_CH_HEADERS,
        )
    except Exception:
        return []
    if resp.status_code != 200:
        return []

    data = resp.json()
    if data.get("query_status") != "ok":
        return []

    # On a miss ThreatFox returns data as the string "No result", not a list —
    # require a genuine non-empty list rather than trusting query_status alone.
    rows = data.get("data")
    if not isinstance(rows, list) or not rows:
        return []

    facts = []
    for ioc in rows:
        facts.append(SourceFact(
            source=FactSource.THREATFOX,
            indicator=EntityRef(type=args.entity_type, value=args.ioc),
            classification=_THREAT_CLASSES.get(
                ioc.get("threat_type", ""), ThreatClassification.UNKNOWN
            ),
            observed_at=now,
            source_confidence=float(ioc.get("confidence_level", 50)) / 100.0,
            expires_at=now + timedelta(hours=1),
            detail={
                "ioc_id": str(ioc.get("id", "")),
                "malware": ioc.get("malware_printable", ""),
                "ioc_type": ioc.get("ioc_type", ""),
            },
        ))
    return facts
