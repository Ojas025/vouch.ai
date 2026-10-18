"""URLhaus (abuse.ch) — malware distribution lookups for a host."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from langchain.tools import tool
from pydantic import BaseModel

from vouch.preprocessing.models import EntityType
from vouch.security import EntityRef
from vouch.tools.facts._shared import (
    _ABUSE_CH_HEADERS, FactSource, SourceFact, ThreatClassification, call, host_of,
)


class UrlhausArgs(BaseModel):
    """Args: the original URL/host string; the bare host is derived internally."""
    url: str

    @classmethod
    def from_entity(cls, entity: EntityRef) -> UrlhausArgs:
        return cls(url=entity.value)

@tool
async def lookup_urlhaus(args: UrlhausArgs) -> list[SourceFact]:
    now = datetime.now(timezone.utc)
    host = host_of(args.url)
    try:
        resp = await call(
            "abuse.ch", "POST", "https://urlhaus-api.abuse.ch/v1/host/",
            data={"host": host},
            headers=_ABUSE_CH_HEADERS,
        )
    except Exception:
        return []
    if resp.status_code != 200:          # 4xx: no listing / bad auth
        return []

    data = resp.json()
    # POST /v1/host/ reports a hit as query_status="ok", a miss as "no_results".
    # (The /v1/url/ endpoint uses "is_listed" — do not cross the two.)
    if data.get("query_status") != "ok":
        return []

    urls = data.get("urls") or []
    first = urls[0] if urls else {}

    return [SourceFact(
        source=FactSource.URLHAUS,
        indicator=EntityRef(type=EntityType.URL, value=args.url),
        classification=ThreatClassification.MALWARE_DISTRIBUTION,
        observed_at=now,
        source_confidence=1.0,
        expires_at=now + timedelta(hours=1),
        detail={
            "urlhaus_reference": data.get("urlhaus_reference", ""),
            "url_count": data.get("url_count", 0),
            "threat": first.get("threat", ""),
            "tags": ", ".join(first.get("tags") or []),
        },
    )]
