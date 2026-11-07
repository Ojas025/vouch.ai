import asyncio
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from random import uniform
from time import monotonic
from typing import Awaitable, Callable
from urllib.parse import quote, urljoin, urlparse

import httpx
from pydantic import BaseModel, Field

from vouch.config import settings
from vouch.logger import get_logger
from vouch.preprocessing.models import EntityType
from vouch.security import EntityRef, Provenance, TrustLevel

logger = get_logger(__name__)

_TOOL_NAME = "entity_intel"
_TOOL_VERSION = "1.0"
_YOUNG_DAYS = 30
_ESTABLISHED_DAYS = 730
_MAX_ATTEMPTS = 3
_BASE_DELAY = 1.0
_MAX_DELAY = 8.0
_MAX_BODY = 2 * 1024 * 1024
_MAX_VALUE_LEN = 2048
_MAX_RESULTS = 10
_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_LIMITS: dict[str, tuple[int, float]] = {
    "abuse.ch": (2, 0.5),
    "rdap.org": (4, 0.25),
    "razorpay.com": (4, 0.25),
    "openphish.com": (1, 0.0),
    "urlscan.io": (2, 1.0),
    "virustotal.com": (2, 1.0),
}
_SEMS: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = {}
_NEXT_OK: dict[str, float] = {}


class SourceName(str, Enum):
    URLHAUS = "urlhaus"
    URLSCAN = "urlscan"
    RDAP = "rdap"
    THREATFOX = "threatfox"
    OPENPHISH = "openphish"
    IFSC = "ifsc"
    VIRUSTOTAL = "virustotal"


class FactSource(str, Enum):
    URLHAUS = "URLHAUS"
    THREATFOX = "THREATFOX"
    OPENPHISH = "OPENPHISH"
    RDAP = "RDAP"
    IFSC = "IFSC"
    URLSCAN = "URLSCAN"
    VIRUSTOTAL = "VIRUSTOTAL"


class ThreatClassification(str, Enum):
    MALWARE_DISTRIBUTION = "MALWARE_DISTRIBUTION"
    C2 = "C2"
    PHISHING = "PHISHING"
    DOMAIN_YOUNG = "DOMAIN_YOUNG"
    DOMAIN_ESTABLISHED = "DOMAIN_ESTABLISHED"
    IFSC_VALID = "IFSC_VALID"
    IFSC_INVALID = "IFSC_INVALID"
    UNKNOWN = "UNKNOWN"
    MALICIOUS_DOMAIN = "MALICIOUS_DOMAIN"
    NO_SCAN_HISTORY = "NO_SCAN_HISTORY"


class FactResult(BaseModel):
    source: FactSource
    classification: ThreatClassification
    observed_at: datetime
    expires_at: datetime | None = None
    source_confidence: float | None = Field(None, ge=0.0, le=1.0)
    detail: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    trust: TrustLevel = TrustLevel.EXTERNAL_SOURCE
    provenance: Provenance


class EntityIntelArgs(BaseModel):
    entity: EntityRef
    source: SourceName | None = None
    investigation_id: str = Field(max_length=64)


class EntityIntelOutput(BaseModel):
    facts: list[FactResult] = Field(default_factory=list)


_VALID: dict[EntityType, re.Pattern] = {
    EntityType.URL: re.compile(r"^(?:https?://)?[^\s<>'\"\\]+$"),
    EntityType.FILE_SHA256: re.compile(r"^[a-fA-F0-9]{64}$"),
    EntityType.IFSC: re.compile(r"^[A-Za-z]{4}0[A-Za-z0-9]{6}$"),
}

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10.0, follow_redirects=False, trust_env=False)
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def host_of(value: str) -> str:
    candidate = value.strip()
    if "://" not in candidate:
        candidate = "http://" + candidate
    host = urlparse(candidate).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _clean(value: object, limit: int = 200) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _gate(name: str) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    entry = _SEMS.get(name)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Semaphore(_LIMITS.get(name, (4, 0.0))[0]))
        _SEMS[name] = entry
    return entry[1]


async def _throttle(name: str) -> None:
    gap = _LIMITS.get(name, (4, 0.0))[1]
    if not gap:
        return
    now = monotonic()
    slot = max(now, _NEXT_OK.get(name, 0.0))
    _NEXT_OK[name] = slot + gap
    delay = slot - now
    if delay > 0:
        await asyncio.sleep(delay)


async def call(name: str, method: str, url: str, **kw) -> httpx.Response:
    sem = _gate(name)
    last_exc: Exception | None = None

    for attempt in range(_MAX_ATTEMPTS):
        await _throttle(name)
        try:
            async with sem:
                async with client().stream(method, url, **kw) as resp:
                    if resp.status_code in _RETRY_STATUSES:
                        last_exc = httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}", request=resp.request, response=resp
                        )
                    else:
                        body = bytearray()
                        async for chunk in resp.aiter_bytes():
                            body += chunk
                            if len(body) > _MAX_BODY:
                                raise httpx.ReadError("response body exceeds limit")
                        headers = httpx.Headers(resp.headers)
                        headers.pop("content-encoding", None)
                        headers.pop("content-length", None)
                        return httpx.Response(
                            resp.status_code,
                            content=bytes(body),
                            headers=headers,
                            request=resp.request,
                        )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
            last_exc = e

        if attempt < _MAX_ATTEMPTS - 1:
            logger.warning(
                "fact source retrying",
                extra={
                    "event": "fact.retry",
                    "component": "tools.entity",
                    "source": name,
                    "attempt": attempt + 1,
                    "error_category": type(last_exc).__name__,
                },
            )
            await asyncio.sleep(uniform(0, min(_MAX_DELAY, _BASE_DELAY * 2 ** attempt)))

    if last_exc is None:
        raise RuntimeError("fact source call failed without a response")

    raise last_exc


async def _redirect(name: str, base: str, resp: httpx.Response, **kw) -> httpx.Response:
    location = resp.headers.get("location", "")
    if not location:
        return resp
    return await call(name, "GET", urljoin(base, location), **kw)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fact(
    source: FactSource,
    classification: ThreatClassification,
    entity: EntityRef,
    observed_at: datetime,
    detail: dict,
    confidence: float | None = None,
    ttl: timedelta | None = None,
) -> FactResult:
    return FactResult(
        source=source,
        classification=classification,
        observed_at=observed_at,
        expires_at=observed_at + ttl if ttl else None,
        source_confidence=confidence,
        detail=detail,
        trust=TrustLevel.EXTERNAL_SOURCE,
        provenance=Provenance(
            producer_type="SERVICE",
            producer_id=source.value,
            tool_name=_TOOL_NAME,
            tool_version=_TOOL_VERSION,
            observed_at=observed_at,
        ),
    )


def _abuse_headers() -> dict[str, str]:
    if not settings.abuse_ch_auth_key:
        return {}
    return {"Auth-Key": settings.abuse_ch_auth_key}


async def lookup_urlhaus(entity: EntityRef) -> list[FactResult]:
    now = _now()
    host = host_of(entity.normalized_value)
    if not host:
        return []
    try:
        resp = await call(
            "abuse.ch",
            "POST",
            "https://urlhaus-api.abuse.ch/v1/host/",
            data={"host": host},
            headers=_abuse_headers(),
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return []
    if data.get("query_status") != "ok":
        return []
    urls = data.get("urls") or []
    first = urls[0] if urls else {}
    raw_tags = first.get("tags")
    tags = [t for t in raw_tags if isinstance(t, str)] if isinstance(raw_tags, list) else []
    return [
        _fact(
            FactSource.URLHAUS,
            ThreatClassification.MALWARE_DISTRIBUTION,
            entity,
            now,
            {
                "urlhaus_reference": _clean(data.get("urlhaus_reference")),
                "url_count": data.get("url_count", 0),
                "threat": _clean(first.get("threat")),
                "tags": ", ".join(_clean(t, 40) for t in tags),
            },
            confidence=1.0,
            ttl=timedelta(hours=1),
        )
    ]


async def lookup_urlscan(entity: EntityRef) -> list[FactResult]:
    now = _now()
    domain = host_of(entity.normalized_value)
    if not domain:
        return []
    headers = {"User-Agent": "vouch-fact-services/0.1"}
    if settings.urlscan_api_key:
        headers["API-Key"] = settings.urlscan_api_key
    try:
        resp = await call(
            "urlscan.io",
            "GET",
            "https://urlscan.io/api/v1/search/",
            params={"q": f"domain:{domain}", "size": 3},
            headers=headers,
        )
        if resp.status_code != 200:
            return []
        results = resp.json().get("results") or []
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return []
    if not results:
        return [
            _fact(
                FactSource.URLSCAN,
                ThreatClassification.NO_SCAN_HISTORY,
                entity,
                now,
                {"domain": domain, "total_scans": 0},
                confidence=0.3,
                ttl=timedelta(hours=24),
            )
        ]
    facts = []
    for r in results[:3]:
        page = r.get("page") or {}
        overall = ((r.get("verdicts") or {}).get("overall") or {})
        malicious = overall.get("malicious")
        facts.append(
            _fact(
                FactSource.URLSCAN,
                ThreatClassification.MALICIOUS_DOMAIN if malicious else ThreatClassification.UNKNOWN,
                entity,
                now,
                {
                    "domain": domain,
                    "title": _clean(page.get("title")),
                    "malicious": bool(malicious),
                    "score": overall.get("score"),
                    "country": _clean(page.get("country"), 64),
                    "asn": _clean(page.get("asn"), 64),
                    "asnname": _clean(page.get("asnname")),
                    "apex_domain": _clean(page.get("apexDomain")),
                    "apex_domain_age_days": page.get("apexDomainAgeDays"),
                    "tls_age_days": page.get("tlsAgeDays"),
                    "tls_issuer": _clean(page.get("tlsIssuer")),
                    "last_scan": _clean((r.get("task") or {}).get("time"), 64),
                },
                confidence=0.7 if malicious else None,
                ttl=timedelta(hours=24),
            )
        )
    return facts


def _registrar(data: dict) -> str:
    for ent in data.get("entities") or []:
        vcard = ent.get("vcardArray") or []
        props = vcard[1] if len(vcard) >= 2 and isinstance(vcard[1], list) else []
        for item in props:
            if isinstance(item, list) and len(item) >= 4 and item[0] == "fn" and isinstance(item[3], str) and item[3]:
                return item[3]
    return ""


def _rdap_dt(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def lookup_rdap(entity: EntityRef) -> list[FactResult]:
    now = _now()
    domain = host_of(entity.normalized_value)
    if not domain:
        return []
    url = f"https://rdap.org/domain/{quote(domain)}"
    try:
        resp = await call("rdap.org", "GET", url, timeout=15.0)
        if resp.status_code in _REDIRECT_STATUSES:
            resp = await _redirect("rdap.org", url, resp, timeout=15.0)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return []
    events = {e.get("eventAction"): e.get("eventDate") for e in (data.get("events") or [])}
    registered_at = _rdap_dt(events.get("registration") or "")
    if registered_at is None:
        return []
    age_days = (now - registered_at).days
    if age_days < _YOUNG_DAYS:
        classification, confidence = ThreatClassification.DOMAIN_YOUNG, 0.8
    elif age_days >= _ESTABLISHED_DAYS:
        classification, confidence = ThreatClassification.DOMAIN_ESTABLISHED, None
    else:
        return []
    return [
        _fact(
            FactSource.RDAP,
            classification,
            entity,
            now,
            {
                "domain": domain,
                "registered_at": registered_at.isoformat(),
                "age_days": age_days,
                "registrar": _clean(_registrar(data)),
            },
            confidence=confidence,
            ttl=timedelta(hours=24),
        )
    ]


_THREAT_CLASSES = {
    "payload_delivery": ThreatClassification.MALWARE_DISTRIBUTION,
    "c2": ThreatClassification.C2,
    "botnet_cc": ThreatClassification.C2,
}


async def lookup_threatfox(entity: EntityRef) -> list[FactResult]:
    now = _now()
    try:
        resp = await call(
            "abuse.ch",
            "POST",
            "https://threatfox-api.abuse.ch/api/v1/",
            json={"query": "search_ioc", "search_term": entity.normalized_value},
            headers=_abuse_headers(),
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return []
    if data.get("query_status") != "ok":
        return []
    rows = data.get("data")
    if not isinstance(rows, list) or not rows:
        return []
    return [
        _fact(
            FactSource.THREATFOX,
            _THREAT_CLASSES.get(row.get("threat_type", ""), ThreatClassification.UNKNOWN),
            entity,
            now,
            {
                "ioc_id": _clean(row.get("id"), 64),
                "malware": _clean(row.get("malware_printable")),
                "ioc_type": _clean(row.get("ioc_type"), 64),
            },
            confidence=min(1.0, float(row.get("confidence_level") or 50) / 100.0),
            ttl=timedelta(hours=1),
        )
        for row in rows[:_MAX_RESULTS]
    ]


_OPENPHISH_TTL = timedelta(hours=6)
_openphish_feed: frozenset[str] = frozenset()
_openphish_refreshed: datetime | None = None
_openphish_lock = asyncio.Lock()


async def _refresh_openphish() -> None:
    global _openphish_feed, _openphish_refreshed
    async with _openphish_lock:
        now = _now()
        if _openphish_refreshed and (now - _openphish_refreshed) < _OPENPHISH_TTL:
            return
        url = "https://openphish.com/feed.txt"
        try:
            resp = await call("openphish.com", "GET", url, timeout=30.0)
            if resp.status_code in _REDIRECT_STATUSES:
                resp = await _redirect("openphish.com", url, resp, timeout=30.0)
            if resp.status_code == 200:
                _openphish_feed = frozenset(line.strip() for line in resp.text.splitlines() if line.strip())
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(
                "openphish feed refresh failed",
                extra={"event": "entity_intel.openphish_refresh", "error_category": type(e).__name__},
            )
        _openphish_refreshed = now


async def lookup_openphish(entity: EntityRef) -> list[FactResult]:
    await _refresh_openphish()
    if entity.normalized_value not in _openphish_feed:
        return []
    now = _now()
    return [
        _fact(
            FactSource.OPENPHISH,
            ThreatClassification.PHISHING,
            entity,
            now,
            {},
            confidence=0.9,
            ttl=_OPENPHISH_TTL,
        )
    ]


async def lookup_ifsc(entity: EntityRef) -> list[FactResult]:
    now = _now()
    code = entity.normalized_value.upper()
    try:
        resp = await call("razorpay.com", "GET", f"https://ifsc.razorpay.com/{quote(code)}")
        if resp.status_code == 404:
            return [
                _fact(
                    FactSource.IFSC,
                    ThreatClassification.IFSC_INVALID,
                    entity,
                    now,
                    {"ifsc": code},
                    confidence=1.0,
                    ttl=timedelta(days=7),
                )
            ]
        if resp.status_code != 200:
            return []
        data = resp.json()
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return []
    return [
        _fact(
            FactSource.IFSC,
            ThreatClassification.IFSC_VALID,
            entity,
            now,
            {
                "ifsc": code,
                "bank": _clean(data.get("BANK")),
                "branch": _clean(data.get("BRANCH")),
                "state": _clean(data.get("STATE")),
                "city": _clean(data.get("CITY")),
            },
            confidence=1.0,
            ttl=timedelta(days=7),
        )
    ]


_VT_BASE = "https://www.virustotal.com/api/v3"


async def lookup_virustotal(entity: EntityRef) -> list[FactResult]:
    if not settings.virustotal_api_key:
        return []
    now = _now()
    try:
        resp = await call(
            "virustotal.com",
            "GET",
            f"{_VT_BASE}/files/{quote(entity.normalized_value)}",
            headers={"x-apikey": settings.virustotal_api_key},
        )
        if resp.status_code != 200:
            return []
        attrs = (resp.json().get("data") or {}).get("attributes") or {}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return []
    stats = attrs.get("last_analysis_stats") or {}
    malicious = int(stats.get("malicious", 0) or 0)
    suspicious = int(stats.get("suspicious", 0) or 0)
    engines = sum(int(v or 0) for v in stats.values())
    if not malicious and not suspicious:
        return []
    ratio = (malicious + suspicious) / engines if engines else 0.0
    return [
        _fact(
            FactSource.VIRUSTOTAL,
            ThreatClassification.MALWARE_DISTRIBUTION,
            entity,
            now,
            {
                "kind": "file",
                "malicious": malicious,
                "suspicious": suspicious,
                "harmless": stats.get("harmless", 0),
                "undetected": stats.get("undetected", 0),
                "engines": engines,
                "reputation": attrs.get("reputation"),
                "meaningful_name": _clean(attrs.get("meaningful_name")),
            },
            confidence=round(min(1.0, ratio), 3),
            ttl=timedelta(hours=6),
        )
    ]


SOURCES: dict[SourceName, Callable[[EntityRef], Awaitable[list[FactResult]]]] = {
    SourceName.URLHAUS: lookup_urlhaus,
    SourceName.URLSCAN: lookup_urlscan,
    SourceName.RDAP: lookup_rdap,
    SourceName.THREATFOX: lookup_threatfox,
    SourceName.OPENPHISH: lookup_openphish,
    SourceName.IFSC: lookup_ifsc,
    SourceName.VIRUSTOTAL: lookup_virustotal,
}

SOURCES_BY_TYPE: dict[EntityType, list[SourceName]] = {
    EntityType.URL: [
        SourceName.URLHAUS,
        SourceName.URLSCAN,
        SourceName.RDAP,
        SourceName.THREATFOX,
        SourceName.OPENPHISH,
    ],
    EntityType.FILE_SHA256: [SourceName.VIRUSTOTAL, SourceName.THREATFOX],
    EntityType.IFSC: [SourceName.IFSC],
}


async def entity_intel(args: EntityIntelArgs) -> EntityIntelOutput:
    valid = SOURCES_BY_TYPE.get(args.entity.type, [])
    pattern = _VALID.get(args.entity.type)
    value = args.entity.normalized_value

    if pattern is not None and (len(value) > _MAX_VALUE_LEN or not pattern.fullmatch(value)):
        logger.warning(
            "rejected invalid entity value",
            extra={"event": "entity_intel.invalid_value", "entity_type": args.entity.type.value},
        )
        return EntityIntelOutput()

    if args.source is not None:
        if args.source not in valid:
            logger.warning(
                "source not valid for entity type",
                extra={"event": "entity_intel.invalid_source", "source": args.source.value, "entity_type": args.entity.type.value},
            )
            return EntityIntelOutput()
        targets = [args.source]
    else:
        targets = valid

    facts: list[FactResult] = []
    errors = 0
    for name in targets:
        try:
            facts.extend(await SOURCES[name](args.entity))
        except Exception as e:
            errors += 1
            logger.warning(
                "fact source failed",
                extra={"event": "entity_intel.source_error", "source": name.value, "error_category": type(e).__name__},
            )

    logger.info(
        "entity intel completed",
        extra={
            "event": "entity_intel.completed",
            "entity_type": args.entity.type.value,
            "source_count": len(targets),
            "fact_count": len(facts),
            "error_count": errors,
        },
    )
    return EntityIntelOutput(facts=facts)
