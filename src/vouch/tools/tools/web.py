import asyncio
from enum import StrEnum
from datetime import datetime, timezone
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import tempfile
import unicodedata
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from pydantic import BaseModel

from vouch.logger import get_logger
from vouch.security import Provenance, TrustLevel
from vouch.utils import uuid7

logger = get_logger(__file__)

# ================================= WEB INSPECT ================================================

class UrlPolicyVerdict(StrEnum):
    OK = "ok"
    INVALID_SYNTAX = "invalid_syntax"
    UNSUPPORTED_SCHEME = "unsupported_scheme"
    NON_STANDARD_PORT = "non_standard_port"
    BLOCKED_PRIVATE_IP = "blocked_private_ip"
    BLOCKED_DNS_FAILURE = "blocked_dns_failure"
    MAX_HOPS_EXCEEDED = "max_hops_exceeded"
    ERROR = "error"

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}

async def validate_url(raw_url: str) -> tuple[UrlPolicyVerdict, str | None]:
    """Strict canonical validation and pre-flight DNS check.
    Rejects parser-differential vectors: backslashes, whitespace, control chars, userinfo.
    Enforces ports 80/443 and verifies host against global IPs.
    """
    if not raw_url or len(raw_url) > MAX_URL_LENGTH:
        return UrlPolicyVerdict.INVALID_SYNTAX, None

    # Reject control chars, newlines, tabs, backslashes, and whitespace
    if any(c in raw_url for c in " \t\r\n\\"):
        return UrlPolicyVerdict.INVALID_SYNTAX, None

    try:
        p = urlparse(raw_url)
    except Exception:
        return UrlPolicyVerdict.INVALID_SYNTAX, None

    if p.scheme not in ALLOWED_SCHEMES:
        return UrlPolicyVerdict.UNSUPPORTED_SCHEME, None

    # Disallow userinfo (e.g. http://user:pass@evil.com)
    if p.username or p.password:
        return UrlPolicyVerdict.INVALID_SYNTAX, None

    if not p.hostname:
        return UrlPolicyVerdict.INVALID_SYNTAX, None

    # Restrict ports to 80 / 443 (default port is None)
    if p.port is not None and p.port not in ALLOWED_PORTS:
        return UrlPolicyVerdict.NON_STANDARD_PORT, None

    # Pre-flight DNS & IP check
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, None)
    except OSError:
        return UrlPolicyVerdict.BLOCKED_DNS_FAILURE, None

    for *_, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            if not ip.is_global:
                return UrlPolicyVerdict.BLOCKED_PRIVATE_IP, None
        except ValueError:
            return UrlPolicyVerdict.INVALID_SYNTAX, None

    return UrlPolicyVerdict.OK, p.geturl()

class RedirectHop(BaseModel):
    url: str
    status_code: int
    location: str | None = None

MAX_REDIRECTS = 10

class WebInspectArgs(BaseModel):
    url: str

class WebInspectOutput(BaseModel):
    initial_url: str
    final_url: str
    blocked_at: str | None = None
    final_status: int | None = None
    safety_status: UrlPolicyVerdict = UrlPolicyVerdict.OK
    hops: list[RedirectHop] = []
    trust: TrustLevel = TrustLevel.DETERMINISTIC
    provenance: Provenance | None = None
    error: str | None = None

MAX_REDIRECTS = 8
REDIRECTS = {301, 302, 303, 307, 308}

async def web_inspect(args: WebInspectArgs) -> WebInspectOutput:
    """Inspect a URL and trace redirections with SSRF prevention and hop limits."""
    hops: list[RedirectHop] = []
    curr = args.url

    async with httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(5.0)
    ) as client:
        try:
            async with asyncio.timeout(15):
                for _ in range(MAX_REDIRECTS + 1):
                    verdict, canonical_url = await validate_url(curr)

                    if verdict != UrlPolicyVerdict.OK:
                        return WebInspectOutput(
                            initial_url=args.url[:MAX_URL_LENGTH],
                            final_url=curr[:MAX_URL_LENGTH],
                            safety_status=verdict,
                            blocked_at=curr[:MAX_URL_LENGTH] if verdict == UrlPolicyVerdict.BLOCKED_PRIVATE_IP else None,
                            hops=hops,
                            error=verdict.value if verdict != UrlPolicyVerdict.BLOCKED_PRIVATE_IP else None
                        )

                    assert canonical_url is not None
                    curr = canonical_url

                    # Stream response to inspect headers without buffering full response body
                    async with client.stream("GET", curr) as r:
                        location = r.headers.get("location")
                        truncated_loc = location[:MAX_URL_LENGTH] if location else None

                        hops.append(RedirectHop(
                            url=curr[:MAX_URL_LENGTH],
                            status_code=r.status_code,
                            location=truncated_loc
                        ))

                        if r.status_code in REDIRECTS and location:
                            curr = urljoin(curr, location)
                            continue

                        return WebInspectOutput(
                            initial_url=args.url[:MAX_URL_LENGTH],
                            final_url=curr[:MAX_URL_LENGTH],
                            hops=hops,
                            final_status=r.status_code,
                            trust=TrustLevel.DETERMINISTIC,
                            provenance=Provenance(
                                producer_type="SERVICE",
                                producer_id="vouch_web_inspect",
                                tool_name="web_inspect",
                                tool_version="1.0",
                                observed_at=datetime.now(timezone.utc),
                            )
                        )

                return WebInspectOutput(
                    safety_status=UrlPolicyVerdict.MAX_HOPS_EXCEEDED,
                    initial_url=args.url[:MAX_URL_LENGTH],
                    final_url=curr[:MAX_URL_LENGTH],
                    hops=hops,
                )

        except (httpx.HTTPError, httpx.InvalidURL, TimeoutError, ValueError) as e:
            return WebInspectOutput(
                safety_status=UrlPolicyVerdict.ERROR,
                final_url=curr[:MAX_URL_LENGTH],
                initial_url=args.url[:MAX_URL_LENGTH],
                hops=hops,
                error=type(e).__name__
            )

# ================================= EXTRACT PAGE CONTENT ================================================

OBSCURA_SEMAPHORE = asyncio.Semaphore(3)
PAGE_LOADED_PATTERN = re.compile(r'^Page loaded:\s*(https?://[^\s]+)\s*-\s*"(.*)"')
MAX_STDOUT_BYTES = 512 * 1024
MAX_STDERR_BYTES = 64 * 1024

async def _run_obscura_safe(
    cmd_args: list[str],
    target_url: str,
    timeout_secs: float = 20.0
) -> tuple[int, str, str, str, str | None]:
    """Runs Obscura in an isolated process group with sanitized environment.
    Returns: (returncode, stdout_content, stderr_clean, final_url, page_title)
    """
    clean_env = {
        "PATH": "/home/ojas/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
    }

    full_args = ["obscura", "fetch"] + cmd_args + ["--", target_url]

    async with OBSCURA_SEMAPHORE:
        proc = await asyncio.create_subprocess_exec(
            *full_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=clean_env,
            start_new_session=True,  # Isolate in own process group for clean killpg
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_secs
            )
        except (TimeoutError, asyncio.CancelledError):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            raise TimeoutError("Obscura execution timed out")

        stdout_str = stdout_bytes[:MAX_STDOUT_BYTES].decode(errors="replace")
        stderr_str = stderr_bytes[:MAX_STDERR_BYTES].decode(errors="replace")

        # Parse Page loaded line from stderr (where Obscura prints status lines)
        final_url = None
        page_title = None
        for line in stderr_str.splitlines():
            m = PAGE_LOADED_PATTERN.match(line.strip())
            if m:
                final_url = m.group(1)
                page_title = m.group(2)
                break

        # Strip unrendered bidi controls and invisible format markers from stdout content
        clean_content = "".join(
            c for c in stdout_str if unicodedata.category(c) not in ("Cf", "Cs")
        )

        return (
            proc.returncode or 0,
            clean_content,
            stderr_str,
            final_url or target_url,
            page_title
        )

class ExtractPageContentArgs(BaseModel):
    url: str

class ExtractedLink(BaseModel):
    url: str
    text: str | None = None
    derived_from: str
    trust: TrustLevel = TrustLevel.UNTRUSTED_ARTIFACT
    provenance: Provenance | None = None

class ExtractPageContentOutput(BaseModel):
    url: str
    final_url: str | None = None
    page_title: str | None = None
    markdown_content: str | None = None
    is_empty_render: bool = False
    links: list[ExtractedLink] = []
    trust: TrustLevel = TrustLevel.UNTRUSTED_ARTIFACT
    provenance: Provenance | None = None
    error: str | None = None

# Bounded to prevent ReDoS on catastrophic attacker-controlled input
MARKDOWN_LINK_PATTERN = re.compile(
    r'\[([^\]\n]{1,200})\]\(([^\s\)\n]{1,2048})\)'
)
RAW_URL_REGEX = re.compile(r'https?://[^\s<>"\'\)]{1,2048}')

MAX_PAGE_CONTENT_CHARS = 100_000
MAX_EXTRACTED_LINKS = 50

def extract_links_from_markdown(content: str, base_url: str) -> list[ExtractedLink]:
    """Extract and normalize links from Markdown content with ReDoS protection."""
    content = content[:MAX_PAGE_CONTENT_CHARS]
    seen_urls: set[str] = set()
    extracted: list[ExtractedLink] = []

    def _normalize(target: str) -> str | None:
        target = target.strip().rstrip(".,;:!?)]}'\"")

        if not target or target.startswith(("javascript:", "mailto:", "tel:", "#")):
            return None

        target = urljoin(base_url, target)
        target, _ = urldefrag(target)
        parsed = urlparse(target)

        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None

        return target[:MAX_URL_LENGTH]

    # 1. Parse markdown links: [anchor text](url)
    for match in MARKDOWN_LINK_PATTERN.finditer(content):

        if len(extracted) >= MAX_EXTRACTED_LINKS:
            break

        text = match.group(1).strip()[:200]
        clean = _normalize(match.group(2))

        if clean and clean not in seen_urls:
            seen_urls.add(clean)

            extracted.append(ExtractedLink(
                url=clean,
                text=text or None,
                derived_from=base_url,
                trust=TrustLevel.UNTRUSTED_ARTIFACT,
                provenance=Provenance(
                    producer_type="SERVICE",
                    producer_id="obscura",
                    tool_name="extract_page_content",
                    tool_version="1.0",
                    observed_at=datetime.now(timezone.utc),
                )
            ))

    # 2. Parse any raw unlinked URLs
    for match in RAW_URL_REGEX.finditer(content):

        if len(extracted) >= MAX_EXTRACTED_LINKS:
            break

        clean = _normalize(match.group(0))

        if clean and clean not in seen_urls:
            seen_urls.add(clean)
            extracted.append(ExtractedLink(
                url=clean,
                text=None,
                derived_from=base_url,
                trust=TrustLevel.UNTRUSTED_ARTIFACT,
                provenance=Provenance(
                    producer_type="SERVICE",
                    producer_id="obscura",
                    tool_name="extract_page_content",
                    tool_version="1.0",
                    observed_at=datetime.now(timezone.utc),
                )
            ))

    return extracted

async def extract_page_content(args: ExtractPageContentArgs) -> ExtractPageContentOutput:
    """Extract page content and links safely via Obscura with adaptive settle."""
    verdict, canonical_url = await validate_url(args.url)

    if verdict != UrlPolicyVerdict.OK:
        return ExtractPageContentOutput(url=args.url, error=verdict.value)

    assert canonical_url is not None

    try:
        rc, content, stderr, final_url, title = await _run_obscura_safe(
            ["--dump", "markdown"],
            canonical_url,
            timeout_secs=20.0
        )

        if rc != 0:
            logger.warning(f"Obscura content extraction failed (rc={rc}): {stderr[:500]}")
            return ExtractPageContentOutput(url=args.url, error=f"obscura_failed_{rc}")

        # Post-execution validation on final landing URL
        final_verdict, verified_final_url = await validate_url(final_url)
        if final_verdict != UrlPolicyVerdict.OK:
            return ExtractPageContentOutput(url=args.url, error=f"redirect_blocked_{final_verdict.value}")

        assert verified_final_url is not None
        is_empty = len(content.strip()) < 30
        links = extract_links_from_markdown(content, verified_final_url)

        return ExtractPageContentOutput(
            url=args.url,
            final_url=verified_final_url,
            page_title=title,
            markdown_content=content,
            is_empty_render=is_empty,
            links=links,
            trust=TrustLevel.UNTRUSTED_ARTIFACT,
            provenance=Provenance(
                producer_type="SERVICE",
                producer_id="obscura",
                tool_name="extract_page_content",
                tool_version="1.0",
                observed_at=datetime.now(timezone.utc),
            )
        )

    except TimeoutError:
        return ExtractPageContentOutput(url=args.url, error="timeout")
    except Exception as e:
        logger.exception("extract_page_content error")
        return ExtractPageContentOutput(url=args.url, error=type(e).__name__)

# =================================== SCREENSHOT PAGE ==============================================

PNG_HEADER = b"\x89PNG\r\n\x1a\n"

def _validate_png(data: bytes) -> tuple[int, int] | None:
    """Validate PNG signature and parse dimensions from IHDR chunk."""
    if len(data) < 24 or not data.startswith(PNG_HEADER):
        return None

    width, height = struct.unpack(">II", data[16:24])

    if width == 0 or height == 0 or width > 8192 or height > 8192:
        return None

    return width, height

def _get_artifacts_dir() -> Path:
    """Lazily resolve and create artifacts storage directory."""
    project_root = Path(__file__).resolve().parents[4]
    artifacts_dir = project_root / "data" / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    return artifacts_dir

class ScreenshotPageArgs(BaseModel):
    url: str

class ScreenshotPageOutput(BaseModel):
    url: str
    artifact_id: str | None = None
    artifact_handle: str | None = None
    sha256_digest: str | None = None
    file_size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    trust: TrustLevel = TrustLevel.UNTRUSTED_ARTIFACT
    provenance: Provenance | None = None
    error: str | None = None

async def screenshot_page(args: ScreenshotPageArgs) -> ScreenshotPageOutput:
    """Capture page screenshot with PNG header verification and content-addressed storage."""
    verdict, canonical_url = await validate_url(args.url)

    if verdict != UrlPolicyVerdict.OK:
        return ScreenshotPageOutput(url=args.url, error=verdict.value)

    assert canonical_url is not None

    with tempfile.TemporaryDirectory(prefix="vouch_scr_") as tmpdir:
        temp_png = Path(tmpdir) / "capture.png"

        try:
            rc, _, stderr, final_url, _ = await _run_obscura_safe(
                ["-s", str(temp_png)],
                canonical_url,
                timeout_secs=20.0
            )

            if rc != 0 or not temp_png.exists():
                logger.warning(f"Obscura screenshot failed (rc={rc}): {stderr[:500]}")
                return ScreenshotPageOutput(url=args.url, error=f"obscura_failed_{rc}")

            # Re-check policy on final redirect destination
            final_verdict, _ = await validate_url(final_url)
            if final_verdict != UrlPolicyVerdict.OK:
                return ScreenshotPageOutput(url=args.url, error=f"redirect_blocked_{final_verdict.value}")

            data = temp_png.read_bytes()
            dimensions = _validate_png(data)

            if not dimensions:
                return ScreenshotPageOutput(url=args.url, error="invalid_png_data")

            width, height = dimensions
            digest = hashlib.sha256(data).hexdigest()
            art_id = str(uuid7())

            # Move validated PNG to content-addressed artifact directory
            artifacts_dir = _get_artifacts_dir()
            final_artifact_path = artifacts_dir / f"{digest}.png"

            if not final_artifact_path.exists():
                shutil.move(str(temp_png), str(final_artifact_path))

            return ScreenshotPageOutput(
                url=args.url,
                artifact_id=art_id,
                artifact_handle=f"art_{digest[:16]}",
                sha256_digest=digest,
                file_size_bytes=len(data),
                width=width,
                height=height,
                trust=TrustLevel.UNTRUSTED_ARTIFACT,
                provenance=Provenance(
                    producer_type="SERVICE",
                    producer_id="obscura",
                    tool_name="screenshot_page",
                    tool_version="1.0",
                    artifact_sha256=digest,
                    observed_at=datetime.now(timezone.utc),
                )
            )

        except TimeoutError:
            return ScreenshotPageOutput(url=args.url, error="timeout")
        except Exception as e:
            logger.exception("screenshot_page error")
            return ScreenshotPageOutput(url=args.url, error=type(e).__name__)
