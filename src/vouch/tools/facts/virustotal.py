"""VirusTotal — multi-engine reputation for a URL or SHA256 file hash."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import base64

from langchain.tools import tool
from pydantic import BaseModel

from vouch.config import settings
from vouch.preprocessing.models import EntityType
from vouch.security import EntityRef
from vouch.tools.facts._shared import FactSource, SourceFact, ThreatClassification, call

_VT_BASE = "https://www.virustotal.com/api/v3"


def _vt_url_id(url: str) -> str:
    """VT identifies URLs by unpadded base64url of the URL."""
    return base64.urlsafe_b64encode(url.encode()).decode().strip("=")


class VirusTotalArgs(BaseModel):
    """Args: the raw value (URL or SHA256) plus the entity type it was extracted as."""
    value: str
    entity_type: EntityType

    @classmethod
    def from_entity(cls, entity: EntityRef) -> VirusTotalArgs:
        return cls(value=entity.value, entity_type=entity.type)

@tool
async def lookup_virustotal(args: VirusTotalArgs) -> list[SourceFact]:
    """Multi-engine reputation. Complements ThreatFox, which is strong on C2/malware
    IOCs but misses the generic-malware case VT's engine panel covers.

    Requires VIRUSTOTAL_API_KEY. Without it, or on a 404, this returns no facts —
    we never manufacture a fact to say "nothing wrong".
    """
    if not settings.virustotal_api_key:
        return []

    now = datetime.now(timezone.utc)
    if args.entity_type == EntityType.FILE_SHA256:
        path, kind = f"/files/{args.value}", "file"
    else:
        path, kind = f"/urls/{_vt_url_id(args.value)}", "url"

    try:
        resp = await call(
            "virustotal.com", "GET", f"{_VT_BASE}{path}",
            headers={"x-apikey": settings.virustotal_api_key},
        )
    except Exception:
        return []
    if resp.status_code != 200:   # 404 = not known to VT; benign
        return []

    attrs = (resp.json().get("data") or {}).get("attributes") or {}
    stats = attrs.get("last_analysis_stats") or {}
    malicious = int(stats.get("malicious", 0) or 0)
    suspicious = int(stats.get("suspicious", 0) or 0)
    engines = sum(int(v or 0) for v in stats.values())

    if not malicious and not suspicious:
        return []

    ratio = (malicious + suspicious) / engines if engines else 0.0
    return [SourceFact(
        source=FactSource.VIRUSTOTAL,
        indicator=EntityRef(type=args.entity_type, value=args.value),
        classification=ThreatClassification.MALWARE_DISTRIBUTION,
        observed_at=now,
        source_confidence=round(min(1.0, ratio), 3),
        expires_at=now + timedelta(hours=6),   # engines re-scan quickly
        detail={
            "kind": kind,
            "malicious": malicious,
            "suspicious": suspicious,
            "harmless": stats.get("harmless", 0),
            "undetected": stats.get("undetected", 0),
            "engines": engines,
            "reputation": attrs.get("reputation"),
            "meaningful_name": str(attrs.get("meaningful_name") or "")[:200],
        },
    )]
