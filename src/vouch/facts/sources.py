from __future__ import annotations
import asyncio
import base64
import time
from datetime import datetime, timezone, timedelta
from random import uniform
from urllib.parse import quote, urlparse

import httpx

from vouch.config import settings
from vouch.facts.models import (
    FactSource, SourceFact, ThreatClassification,
)
from vouch.preprocessing.models import EntityType
from vouch.security.models import EntityRef

_ABUSE_CH_HEADERS = {"Auth-Key": settings.abuse_ch_auth_key}

# One lazily-created shared client. All sources reuse it.
#
# follow_redirects=True is required, not optional: rdap.org is an aggregator that always
# 302s to the authoritative registry, and openphish.com now 302s to a raw GitHub feed.
# Without it both return the redirect itself, which is neither in _RETRY_STATUSES nor a
# 200, so every RDAP/OpenPhish lookup silently returns [].
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    return _client


# --- Rate limiting + retry -------------------------------------------------
#
# Per-source bounds so one entity's fan-out (5 sources at once) can't hammer a
# single upstream, and so concurrent investigations don't stack on top of it.
#
# The house `@retry` decorator (vouch.utils.retry) is async-aware and already
# used in branches/router.py, so reuse it rather than writing a new one. It
# retries *every* Exception though. That is wrong for a 4xx: a 404 from Razorpay
# is a *deterministic answer* (no such IFSC), not a transient failure, and
# retrying it three times just adds latency. So we do NOT blanket-wrap the call
# in `@retry` — instead `call` retries a curated set of transient conditions
# itself, using the same backoff+jitter formula as the house decorator. This
# keeps the transient-vs-permanent decision in one visible place.

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
    Strips userinfo, port, path, and a leading `www.`.

    The `www.` strip matters for RDAP specifically: the rdap.org aggregator rejects
    non-registrable names with a 400 (www.wikipedia.org -> 400, wikipedia.org -> 200).
    URLhaus /host/ is happy either way.
    """
    candidate = value.strip()
    if "://" not in candidate:
        candidate = "http://" + candidate
    host = urlparse(candidate).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host


# --- URLhaus (malware distribution) ---

async def lookup_urlhaus(entity: EntityRef) -> list[SourceFact]:
    now = datetime.now(timezone.utc)
    try:
        resp = await call(
            "abuse.ch", "POST", "https://urlhaus-api.abuse.ch/v1/host/",
            data={"host": host_of(entity.value)},
            headers=_ABUSE_CH_HEADERS,
        )
    except Exception:
        return []
    if resp.status_code != 200:          # 4xx: no listing / bad auth
        return []

    data = resp.json()
    # POST /v1/host/ reports a hit as query_status="ok" (with url_count/urls/
    # urlhaus_reference); a miss is "no_results". Note this differs from the
    # /v1/url/ endpoint, which uses "is_listed" — do not cross the two.
    if data.get("query_status") != "ok":
        return []

    urls = data.get("urls") or []
    first = urls[0] if urls else {}

    return [SourceFact(
        source=FactSource.URLHAUS,
        indicator=entity,
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


# --- ThreatFox (C2 / IOC malware) ---

_THREAT_CLASSES = {
    "payload_delivery": ThreatClassification.MALWARE_DISTRIBUTION,
    "c2": ThreatClassification.C2,
    "botnet_cc": ThreatClassification.C2,
}


async def lookup_threatfox(entity: EntityRef) -> list[SourceFact]:
    now = datetime.now(timezone.utc)
    try:
        resp = await call(
            "abuse.ch", "POST", "https://threatfox-api.abuse.ch/api/v1/",
            json={"query": "search_ioc", "search_term": entity.value},
            headers=_ABUSE_CH_HEADERS,
        )
    except Exception:
        return []
    if resp.status_code != 200:
        return []

    data = resp.json()
    if data.get("query_status") != "ok":
        return []

    # Guard the shape: on a miss ThreatFox returns query_status="no_result" *and*
    # data as the string "No result" rather than a list. Relying on query_status alone
    # is fragile, so only proceed when `data` is genuinely a non-empty list.
    rows = data.get("data")
    if not isinstance(rows, list) or not rows:
        return []

    facts = []
    for ioc in rows:
        facts.append(SourceFact(
            source=FactSource.THREATFOX,
            indicator=entity,
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


# --- urlscan.io (scan history, page content, hosting context) ---

_URLSCAN_SEARCH = "https://urlscan.io/api/v1/search/"

async def lookup_urlscan(entity: EntityRef) -> list[SourceFact]:
    """What the domain is actually serving: page title, hosting country/ASN, TLS issuer
    and certificate age, plus any urlscan verdict.

    Works unauthenticated on the free tier. Note that `verdict:` search filtering is a
    PAID feature and returns HTTP 403, so we query by `domain:` only and read the
    verdicts off each result. A miss is HTTP 200 with total=0, not a 404.
    """
    now = datetime.now(timezone.utc)
    domain = host_of(entity.value)
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
        # Never scanned by anyone. This is NOT "verified clean", so it gets its own
        # classification rather than being silently dropped.
        return [SourceFact(
            source=FactSource.URLSCAN,
            indicator=entity,
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
            indicator=entity,
            classification=(
                ThreatClassification.MALICIOUS_DOMAIN if malicious
                else ThreatClassification.UNKNOWN
            ),
            observed_at=now,
            # urlscan verdicts are heuristic — never claim full confidence.
            source_confidence=0.7 if malicious else None,
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


# --- VirusTotal (multi-engine reputation for URL / SHA256) ---

_VT_BASE = "https://www.virustotal.com/api/v3"

def _vt_url_id(url: str) -> str:
    """VT identifies URLs by unpadded base64url of the URL."""
    return base64.urlsafe_b64encode(url.encode()).decode().strip("=")

async def lookup_virustotal(entity: EntityRef) -> list[SourceFact]:
    """Multi-engine reputation. Complements ThreatFox, which is strong on C2/malware
    IOCs but misses the generic-malware case VT's engine panel covers.

    Requires VIRUSTOTAL_API_KEY. Without it, or on a 404, this returns no facts —
    consistent with the rule that we never manufacture a fact to say "nothing wrong".
    """
    if not settings.virustotal_api_key:
        return []

    now = datetime.now(timezone.utc)
    if entity.type == EntityType.FILE_SHA256:
        path, kind = f"/files/{entity.value}", "file"
    else:
        path, kind = f"/urls/{_vt_url_id(entity.value)}", "url"

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
        indicator=entity,
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


# --- OpenPhish (phishing feed, bulk-downloaded) ---

_openphish_feed: frozenset[str] = frozenset()
_openphish_refreshed: datetime | None = None
_openphish_lock = asyncio.Lock()

OPENPHISH_TTL = timedelta(hours=6)


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


async def lookup_openphish(entity: EntityRef) -> list[SourceFact]:
    await _refresh_openphish()
    if entity.value not in _openphish_feed:
        return []

    now = datetime.now(timezone.utc)
    return [SourceFact(
        source=FactSource.OPENPHISH,
        indicator=entity,
        classification=ThreatClassification.PHISHING,
        observed_at=now,
        source_confidence=0.9,
        expires_at=now + OPENPHISH_TTL,
        detail={},
    )]


# --- RDAP (domain age + registrar) ---

_YOUNG_DAYS = 30
_ESTABLISHED_DAYS = 730   # 2 years


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


async def lookup_rdap(entity: EntityRef) -> list[SourceFact]:
    now = datetime.now(timezone.utc)
    domain = host_of(entity.value)
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
        indicator=entity,
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


# --- IFSC (Razorpay resolution) ---
#
# IFSC is the one source where a 404 is a *finding*, not an error: Razorpay returns
# 404 when the code matches no live branch, which is exactly the IFSC_INVALID signal.
# So this source inspects the status itself instead of treating it as "no data".

async def lookup_ifsc(entity: EntityRef) -> list[SourceFact]:
    now = datetime.now(timezone.utc)
    code = entity.value.upper()

    try:
        resp = await call("razorpay.com", "GET", f"https://ifsc.razorpay.com/{code}")
    except Exception:
        return []

    if resp.status_code == 404:
        return [SourceFact(
            source=FactSource.IFSC,
            indicator=entity,
            classification=ThreatClassification.IFSC_INVALID,
            observed_at=now,
            source_confidence=1.0,
            expires_at=now + timedelta(days=7),
            detail={"ifsc": code},
        )]

    if resp.status_code != 200:
        return []

    data = resp.json()
    return [SourceFact(
        source=FactSource.IFSC,
        indicator=entity,
        classification=ThreatClassification.IFSC_VALID,
        observed_at=now,
        source_confidence=1.0,
        expires_at=now + timedelta(days=7),
        detail={
            "ifsc": code,
            "bank": data.get("BANK", ""),
            "branch": data.get("BRANCH", ""),
            "state": data.get("STATE", ""),
            "city": data.get("CITY", ""),
        },
    )]


# --- Wiring ---
#
# core     = URLhaus + urlscan + RDAP. Small, fast, no quota burn.
# extended = adds ThreatFox (C2/malware IOCs) and OpenPhish (brand-targeted phishing).
#
# Two entity types are deliberately unmapped rather than given a weak heuristic:
#   UPI_VPA — no public validation API exists (Razorpay/Paytm/Cashfree are merchant-only).
#   PHONE   — carrier/line-type data is licensed from carriers, so every provider costs
#             money (Twilio $0.008/lookup; Numverify's free tier is non-commercial only).
#             Revisit once monthly volume is known.

def build_registry(profile: str | None = None) -> dict[EntityType, list]:
    profile = profile or settings.fact_profile

    url: list = [lookup_urlhaus, lookup_urlscan, lookup_rdap]
    if profile == "extended":
        url += [lookup_threatfox, lookup_openphish]

    sha: list = [lookup_virustotal]
    if profile == "extended":
        sha.append(lookup_threatfox)

    return {
        EntityType.URL:         url,
        EntityType.FILE_SHA256: sha,
        EntityType.IFSC:        [lookup_ifsc],
    }


REGISTRY: dict[EntityType, list] = build_registry()
