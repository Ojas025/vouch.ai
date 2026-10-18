"""OpenPhish — bulk-downloaded phishing feed membership check for a URL."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from langchain.tools import tool

from vouch.preprocessing.models import EntityType
from vouch.security import EntityRef
from vouch.tools.facts._shared import FactSource, SourceFact, ThreatClassification, call

_openphish_feed: frozenset[str] = frozenset()
_openphish_refreshed: datetime | None = None
_openphish_lock = asyncio.Lock()

OPENPHISH_TTL = timedelta(hours=6)


class OpenPhishArgs(BaseModel):
    """Args: the original URL string, matched verbatim against the feed."""
    url: str

    @classmethod
    def from_entity(cls, entity: EntityRef) -> OpenPhishArgs:
        return cls(url=entity.value)


async def _refresh_openphish() -> None:
    global _openphish_feed, _openphish_refreshed
    async with _openphish_lock:
        now = datetime.now(timezone.utc)
        if _openphish_refreshed and (now - _openphish_refreshed) < OPENPHISH_TTL:
            return
        try:
            resp = await call(
                "openphish.com", "GET", "https://openphish.com/feed.txt", timeout=30.0,
            )
            if resp.status_code == 200:
                _openphish_feed = frozenset(
                    line.strip() for line in resp.text.splitlines() if line.strip()
                )
                _openphish_refreshed = now
        except Exception:
            pass  # degrade gracefully — a stale feed beats no feed


@tool
async def lookup_openphish(args: OpenPhishArgs) -> list[SourceFact]:
    await _refresh_openphish()
    if args.url not in _openphish_feed:
        return []

    now = datetime.now(timezone.utc)
    return [SourceFact(
        source=FactSource.OPENPHISH,
        indicator=EntityRef(type=EntityType.URL, value=args.url),
        classification=ThreatClassification.PHISHING,
        observed_at=now,
        source_confidence=0.9,
        expires_at=now + OPENPHISH_TTL,
        detail={},
    )]
