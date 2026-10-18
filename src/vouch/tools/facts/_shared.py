"""Shared infrastructure for the fact tools: models, cache, HTTP helpers, and the
lookup() service. Individual sources live in their own module (urlhaus.py, ...), each
exposing a <Tool>Args schema and an async tool that emits SourceFacts."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from enum import Enum
from random import uniform
from typing import Awaitable, Callable
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel, Field

from vouch.config import settings
from vouch.preprocessing.models import EntityType
from vouch.security import EntityRef


# --- Models ---


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
    MALICIOUS_DOMAIN     = "MALICIOUS_DOMAIN"   # positive verdict from urlscan or VT
    NO_SCAN_HISTORY      = "NO_SCAN_HISTORY"    # never scanned — weak, and NOT "verified clean"


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


# --- Cache ---

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


# --- HTTP helpers: shared client, rate limiting, retry ---

_ABUSE_CH_HEADERS = {"Auth-Key": settings.abuse_ch_auth_key}

# One lazily-created shared client. All sources reuse it.
# follow_redirects=True is required: rdap.org and openphish.com always 302 to the
# authoritative source; without it both return the redirect body, which is neither a
# 200 nor a retryable status, so every such lookup silently returns [].
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    return _client


# Per-source concurrency + spacing so one entity's fan-out can't hammer a single
# upstream, and so concurrent investigations don't stack on top of it.
#
# The house `@retry` decorator retries *every* Exception, which is wrong for a 4xx:
# a 404 from Razorpay is a deterministic answer (no such IFSC), not a transient
# failure. So `call` retries a curated set of transient conditions itself, using the
# same backoff+jitter formula as the house decorator.

# Statuses worth retrying: rate-limited and transient server-side failures.
_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

MAX_ATTEMPTS = 3
BASE_DELAY = 1.0
MAX_DELAY = 8.0

# name -> (max concurrent in-flight, min seconds between call starts)
_LIMITS: dict[str, tuple[int, float]] = {
    "abuse.ch":        (2, 0.5),
    "rdap.org":        (4, 0.25),
    "razorpay.com":    (4, 0.25),
    "openphish.com":   (1, 0.0),   # bulk feed; gated by the 6h refresh lock instead
    "urlscan.io":      (2, 1.0),
    "virustotal.com":  (2, 1.0),   # free tier: 4 req/s
}

_sems: dict[str, asyncio.Semaphore] = {}
_next_ok: dict[str, float] = {}


def _gate(name: str) -> asyncio.Semaphore:
    sem = _sems.get(name)
    if sem is None:
        sem = _sems[name] = asyncio.Semaphore(_LIMITS.get(name, (4, 0.0))[0])
    return sem


async def _throttle(name: str) -> None:
    """Sleep so that calls to one upstream start no faster than its configured gap.

    The slot is *reserved* before awaiting, otherwise concurrent callers all read the
    same stale _next_ok and only the first gap is enforced.
    """
    gap = _LIMITS.get(name, (4, 0.0))[1]
    if not gap:
        return
    now = time.monotonic()
    slot = max(now, _next_ok.get(name, 0.0))
    _next_ok[name] = slot + gap          # reserve synchronously
    delay = slot - now
    if delay > 0:
        await asyncio.sleep(delay)


async def call(name: str, method: str, url: str, **kw) -> httpx.Response:
    """Rate-limited, retried HTTP call.

    Retries only transient conditions (timeout, connect error, 429/5xx) with the
    same exponential backoff + jitter as `vouch.utils.retry`. A 4xx is returned to
    the caller *without* retrying, because for these APIs a 4xx is a real answer
    (e.g. IFSC 404 = no such branch) that the source must interpret.
    """
    sem = _gate(name)
    last_exc: Exception | None = None

    for attempt in range(MAX_ATTEMPTS):
        await _throttle(name)
        try:
            async with sem:
                resp = await client().request(method, url, **kw)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
        else:
            if resp.status_code not in _RETRY_STATUSES:
                return resp  # success OR a non-retryable 4xx — caller decides
            last_exc = httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )

        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep(uniform(0, min(MAX_DELAY, BASE_DELAY * 2 ** attempt)))

    raise last_exc  # type: ignore[misc]


def host_of(value: str) -> str:
    """Bare hostname from a URL. Falls back gracefully for a bare host with no scheme.
    Strips userinfo, port, path, and a leading `www.` — the rdap.org aggregator
    rejects non-registrable names with a 400 (www.wikipedia.org -> 400)."""
    candidate = value.strip()
    if "://" not in candidate:
        candidate = "http://" + candidate
    host = urlparse(candidate).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host


# --- Service ---


def _bind(name: str, args_model: type[BaseModel], handler: Callable[..., Awaitable[list[SourceFact]]]):
    """Adapt a schema'd tool (Args -> list[SourceFact]) to the entity -> list[SourceFact]
    contract the lookup() service and REGISTRY use, so tools stay arg-schema driven while
    the fan-out keeps working off an EntityRef."""

    async def run(entity: EntityRef) -> list[SourceFact]:
        return await handler(args_model.from_entity(entity))

    run.__name__ = name
    return run


async def _guard(src, entity: EntityRef) -> tuple[list[SourceFact], str]:
    """Run one source, converting any failure into an empty result + error string so one bad
    source can never take down the batch."""
    try:
        return await src(entity), ""
    except Exception as e:
        return [], str(e)


async def lookup(
    entity: EntityRef,
    investigation_id: str,
    redis,
    registry: dict | None = None,
) -> FactServiceResult:
    """Fan out across every source registered for this entity type, in parallel, merge the
    results, and cache them."""
    sources = (registry if registry is not None else REGISTRY).get(entity.type, [])
    query = FactQuery(entity=entity, investigation_id=investigation_id)
    t0 = time.monotonic()

    if not sources:
        return FactServiceResult(
            query=query,
            status=FactLookupStatus.SKIPPED,
            duration_ms=(time.monotonic() - t0) * 1000,
        )

    cached = await get_facts(redis, entity)
    if cached is not None:
        return FactServiceResult(
            query=query,
            status=FactLookupStatus.CACHED,
            facts=cached,
            cache_hit=True,
            duration_ms=(time.monotonic() - t0) * 1000,
        )

    results = await asyncio.gather(
        *(_guard(src, entity) for src in sources), return_exceptions=True
    )

    facts: list[SourceFact] = []
    errors: list[str] = []
    for src, res in zip(sources, results):
        if isinstance(res, BaseException):
            # CancelledError is a BaseException that _guard doesn't catch; it must
            # not abort the batch.
            errors.append(f"{src.__name__}: {res}")
            continue
        src_facts, err = res
        facts.extend(src_facts)
        if err:
            errors.append(f"{src.__name__}: {err}")

    if facts:
        # Single write, TTL bounded by the shortest-lived fact.
        await set_facts(redis, entity, facts, merged_ttl(facts))

    return FactServiceResult(
        query=query,
        status=FactLookupStatus.HIT if facts else FactLookupStatus.MISS,
        facts=facts,
        errors=errors,
        duration_ms=(time.monotonic() - t0) * 1000,
    )


def to_evidence_drafts(result: FactServiceResult) -> list:
    """Convert a lookup result into EvidenceDrafts ready for the ledger writer."""
    from vouch.ledger.models import EvidenceDraft, SourceFactPayload
    from vouch.preprocessing.models import Modality
    from vouch.security import Provenance, TrustLevel

    drafts = []
    for fact in result.facts:
        if fact.is_expired:
            continue

        drafts.append(EvidenceDraft(
            investigation_id=result.query.investigation_id,
            status="ok",
            # Facts derived from a text message are TEXT-modality regardless of entity type.
            modality=Modality.TEXT,
            subject=None,
            payload=SourceFactPayload(
                source=fact.source.value,
                classification=fact.classification.value,
                source_confidence=fact.source_confidence,
                detail=fact.detail,
            ),
            trust=TrustLevel.EXTERNAL_SOURCE,
            provenance=Provenance(
                producer_type="SERVICE",
                producer_id=fact.source.value,
                tool_name=f"fact_service:{fact.source.value.lower()}",
                tool_version="1.0",
                observed_at=fact.observed_at,
            ),
        ))
    return drafts


# --- Registry wiring ---

# core     = URLhaus + urlscan + RDAP. Small, fast, no quota burn.
# extended = adds ThreatFox (C2/malware IOCs) and OpenPhish (brand-targeted phishing).
#
# UPI_VPA and PHONE are deliberately unmapped rather than given a weak heuristic:
# no public UPI validation API exists, and carrier/line-type data is licensed
# (paid) from carriers for every provider.

def build_registry(profile: str | None = None) -> dict[EntityType, list]:
    # Imported here (not at module top) to avoid a circular import: each tool module
    # imports this module for SourceFact/call/host_of.
    from vouch.tools.facts import (
        ifsc, openphish, rdap, threatfox, urlhaus, urlscan, virustotal,
    )

    profile = profile or settings.fact_profile

    url: list = [
        _bind("lookup_urlhaus", urlhaus.UrlhausArgs, urlhaus.lookup_urlhaus),
        _bind("lookup_urlscan", urlscan.UrlscanArgs, urlscan.lookup_urlscan),
        _bind("lookup_rdap", rdap.RdapArgs, rdap.lookup_rdap),
    ]
    if profile == "extended":
        url += [
            _bind("lookup_threatfox", threatfox.ThreatFoxArgs, threatfox.lookup_threatfox),
            _bind("lookup_openphish", openphish.OpenPhishArgs, openphish.lookup_openphish),
        ]

    sha: list = [
        _bind("lookup_virustotal", virustotal.VirusTotalArgs, virustotal.lookup_virustotal),
    ]
    if profile == "extended":
        sha.append(_bind("lookup_threatfox", threatfox.ThreatFoxArgs, threatfox.lookup_threatfox))

    return {
        EntityType.URL:         url,
        EntityType.FILE_SHA256: sha,
        EntityType.IFSC:        [_bind("lookup_ifsc", ifsc.IFSCArgs, ifsc.lookup_ifsc)],
    }


REGISTRY: dict[EntityType, list] = build_registry()
