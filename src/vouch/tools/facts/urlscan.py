"""urlscan.io — scan history, page content, and hosting context for a domain."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from langchain.tools import tool
from pydantic import BaseModel

from vouch.config import settings
from vouch.preprocessing.models import EntityType
from vouch.security import EntityRef
from vouch.tools.facts._shared import (
    FactSource, SourceFact, ThreatClassification, call, host_of,
)

_URLSCAN_SEARCH = "https://urlscan.io/api/v1/search/"


class UrlscanArgs(BaseModel):
    """Args: the original URL string; the bare domain is derived internally."""
    url: str

    @classmethod
    def from_entity(cls, entity: EntityRef) -> UrlscanArgs:
        return cls(url=entity.value)

@tool
async def lookup_urlscan(args: UrlscanArgs) -> list[SourceFact]:
    """What the domain is actually serving: page title, hosting country/ASN, TLS issuer
    and certificate age, plus any urlscan verdict.

    Works unauthenticated on the free tier. `verdict:` search filtering is a PAID
    feature (HTTP 403), so we query by `domain:` only and read the verdicts off each
    result. A miss is HTTP 200 with total=0, not a 404.
    """
    now = datetime.now(timezone.utc)
    domain = host_of(args.url)
    if not domain:
        return []

    headers = {"User-Agent": "vouch-fact-services/0.1"}
    if settings.urlscan_api_key:
        headers["API-Key"] = settings.urlscan_api_key

    try:
        resp = await call(
            "urlscan.io", "GET", _URLSCAN_SEARCH,
            params={"q": f"domain:{domain}", "size": 3},
            headers=headers,
        )
    except Exception:
        return []
    if resp.status_code != 200:
        return []

    results = resp.json().get("results") or []
    if not results:
        # Never scanned by anyone — NOT "verified clean", so it gets its own
        # classification rather than being silently dropped.
        return [SourceFact(
            source=FactSource.URLSCAN,
            indicator=EntityRef(type=EntityType.URL, value=args.url),
            classification=ThreatClassification.NO_SCAN_HISTORY,
            observed_at=now,
            source_confidence=0.3,
            expires_at=now + timedelta(hours=24),
            detail={"domain": domain, "total_scans": 0},
        )]

    facts = []
    for r in results[:3]:
        page = r.get("page") or {}
        overall = ((r.get("verdicts") or {}).get("overall") or {})
        malicious = overall.get("malicious")
        facts.append(SourceFact(
            source=FactSource.URLSCAN,
            indicator=EntityRef(type=EntityType.URL, value=args.url),
            classification=(
                ThreatClassification.MALICIOUS_DOMAIN if malicious
                else ThreatClassification.UNKNOWN
            ),
            observed_at=now,
            source_confidence=0.7 if malicious else None,   # verdicts are heuristic
            expires_at=now + timedelta(hours=24),
            detail={
                "domain": domain,
                "title": str(page.get("title") or "")[:200],
                "malicious": bool(malicious),
                "score": overall.get("score"),
                "country": page.get("country", ""),
                "asn": page.get("asn", ""),
                "asnname": page.get("asnname", ""),
                "apex_domain": page.get("apexDomain", ""),
                "apex_domain_age_days": page.get("apexDomainAgeDays"),
                "tls_age_days": page.get("tlsAgeDays"),
                "tls_issuer": page.get("tlsIssuer", ""),
                "last_scan": (r.get("task") or {}).get("time", ""),
            },
        ))
    return facts
