from __future__ import annotations
import json
from datetime import datetime, timezone

from vouch.facts.models import SourceFact
from vouch.security.models import EntityRef

# Per-source TTL defaults. Fact sources use these to set expires_at; set_facts uses the
# derived minimum, not these values directly.
DEFAULT_TTLS: dict[str, int] = {
    "URLHAUS":    3600,    # 1 hour  — feed updated ~hourly
    "THREATFOX":  3600,    # 1 hour
    "OPENPHISH":  21600,   # 6 hours — bulk feed
    "RDAP":       86400,   # 24 hours — registrations change slowly
    "IFSC":       604800,  # 7 days  — IFSC codes are stable
    "URLSCAN":    86400,   # 24 hours
    "VIRUSTOTAL": 21600,   # 6 hours — engines re-scan quickly
}


def _key(entity: EntityRef) -> str:
    return f"vouch:fact:{entity.type.value}:{entity.value}"


def merged_ttl(facts: list[SourceFact]) -> int:
    """Seconds until the *shortest-lived* member fact expires. Never cache past any member's
    expiry. Facts with no expiry are bounded at 1 hour."""
    now = datetime.now(timezone.utc)
    remaining = [
        3600 if f.expires_at is None else max(0, int((f.expires_at - now).total_seconds()))
        for f in facts
    ]
    return min(remaining) if remaining else 0


async def get_facts(redis, entity: EntityRef) -> list[SourceFact] | None:
    raw = await redis.get(_key(entity))
    if raw is None:
        return None
    return [SourceFact.model_validate(f) for f in json.loads(raw)]


async def set_facts(redis, entity: EntityRef, facts: list[SourceFact], ttl: int) -> None:
    """Cache the merged fact list under a single TTL (seconds). Caller supplies the TTL already
    bounded by the shortest-lived member fact."""
    if not facts or ttl <= 0:
        return
    payload = json.dumps([f.model_dump(mode="json") for f in facts])
    await redis.set(_key(entity), payload, ex=ttl)
