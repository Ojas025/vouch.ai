"""Unit tests for the web tools suite.

Derived from docs/security/web.md (§1-§12) and the web tools test plan.
Offline by construction: httpx uses MockTransport and getaddrinfo is stubbed on
the event loop. _run_obscura_safe tests run the real Obscura binary against a
local HTTP server and skip when the binary is unavailable.
"""

import asyncio
import contextlib
import hashlib
import ipaddress
import logging
import shutil
import socket
import struct
import tempfile
import threading
import time
import unicodedata
import uuid
from asyncio.base_events import BaseEventLoop
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from tavily import BadRequestError, InvalidAPIKeyError, MissingAPIKeyError, UsageLimitExceededError
from tavily.errors import ForbiddenError

import vouch.tools.tools.web as web
from vouch.bootstrap.obscura import ensure_obscura
from vouch.security import TrustLevel
from vouch.tools.tools.web import (
    MAX_REDIRECTS,
    ExtractPageContentArgs,
    ScreenshotPageArgs,
    UrlPolicyVerdict,
    WebInspectArgs,
    WebSearchArgs,
    WebSearchError,
    extract_links_from_markdown,
    extract_page_content,
    screenshot_page,
    validate_url,
    web_inspect,
    web_search,
)

GLOBAL_IPV4 = "93.184.216.34"
REDOS_TIME_BUDGET_SECS = 0.25
LONG_PAGE_BODY = b"# Report\n\nBody text with enough characters to render a real page."


# ============================== Helpers ==============================


def _patch_dns(monkeypatch, ips=None, recorder=None):
    """Stub event-loop DNS: literals pass through, hostnames resolve per ips.

    ips=None resolves hostnames to a fixed public IP; ips="fail" raises OSError;
    an iterable returns those addresses in order (multi-A-record scenarios).
    """
    async def fake_getaddrinfo(self, host, port, *args, **kwargs):
        if recorder is not None:
            recorder.append(host)
        if ips == "fail":
            raise OSError(-2, "Name or service not known")
        if ips is None:
            try:
                ipaddress.ip_address(host)
                resolved = [host]
            except ValueError:
                resolved = [GLOBAL_IPV4]
        else:
            resolved = list(ips)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in resolved]

    monkeypatch.setattr(BaseEventLoop, "getaddrinfo", fake_getaddrinfo)


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """Default offline DNS for every test; V1/V3 re-patch with richer stubs."""
    _patch_dns(monkeypatch)


@pytest.fixture(autouse=True)
def _obscura_path(monkeypatch):
    """Spawn-contract tests assert argv uses the configured path; keep it offline."""
    monkeypatch.setattr(web.settings, "obscura_path", Path("obscura"))


def _mock_httpx_client(monkeypatch, handler):
    """Route every httpx.AsyncClient through an in-process MockTransport."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _fake_obscura(monkeypatch, stdout=b"", stderr=b"", returncode=0):
    """Replace Obscura's subprocess with an in-process fake process."""

    async def fake_exec(*args, **kwargs):
        return FakeProcess(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


class FakeProcess:
    """Minimal asyncio subprocess stand-in."""

    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self.pid = 4242
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.hang = hang
        self.wait_called = False
        self.communicate_started = asyncio.Event()

    async def communicate(self):
        self.communicate_started.set()
        if self.hang:
            await asyncio.Event().wait()
        return self.stdout, self.stderr

    async def wait(self):
        self.wait_called = True
        return self.returncode


def _png_bytes(width, height, magic=b"\x89PNG\r\n\x1a\n"):
    """Byte layout _validate_png inspects: magic + IHDR dims at offset 16."""
    return magic + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x00" * 8


def _patch_artifacts_dir(monkeypatch, tmp_path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    monkeypatch.setattr(web, "_get_artifacts_dir", lambda: artifacts_dir)
    return artifacts_dir


def _obscura_writes_capture(png_bytes, returncode=0, final_url=None):
    """Fake _run_obscura_safe that writes a capture file at the -s path."""

    async def fake(cmd_args, target_url, timeout_secs=20.0):
        capture_path = Path(cmd_args[cmd_args.index("-s") + 1])
        if returncode == 0:
            capture_path.write_bytes(png_bytes)
        return (returncode, "", "obscura failed", final_url or target_url, None)

    return fake


def _record_tempdirs(monkeypatch):
    tempdirs = []
    real_tempdir = tempfile.TemporaryDirectory

    def recording_tempdir(*args, **kwargs):
        tmpdir = real_tempdir(*args, **kwargs)
        tempdirs.append(tmpdir.name)
        return tmpdir

    monkeypatch.setattr(tempfile, "TemporaryDirectory", recording_tempdir)
    return tempdirs


def _patch_tavily(monkeypatch, response=None, side_effect=None):
    search = AsyncMock(return_value=response, side_effect=side_effect)
    monkeypatch.setattr(web, "tavily_client", SimpleNamespace(search=search))
    return search


def _search_item(**overrides):
    item = {
        "title": "A title",
        "url": "http://site.test/page",
        "content": "A snippet",
        "score": 0.9,
        "published_date": "2024-05-01",
    }
    item.update(overrides)
    return item


# ============================== validate_url ==============================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", UrlPolicyVerdict.INVALID_SYNTAX),
        ("https://example.com/" + "a" * 2048, UrlPolicyVerdict.INVALID_SYNTAX),
        ("https://exa mple.com/", UrlPolicyVerdict.INVALID_SYNTAX),
        ("https://example.com/a\tb", UrlPolicyVerdict.INVALID_SYNTAX),
        ("https://example.com/a\rb", UrlPolicyVerdict.INVALID_SYNTAX),
        ("https://example.com/a\nb", UrlPolicyVerdict.INVALID_SYNTAX),
        ("https://google.com\\@127.0.0.1", UrlPolicyVerdict.INVALID_SYNTAX),
        ("http://user:pass@legit.com", UrlPolicyVerdict.INVALID_SYNTAX),
        ("http://user@example.com", UrlPolicyVerdict.INVALID_SYNTAX),
        ("http:///path", UrlPolicyVerdict.INVALID_SYNTAX),
        ("https://", UrlPolicyVerdict.INVALID_SYNTAX),
        ("ftp://example.com/file", UrlPolicyVerdict.UNSUPPORTED_SCHEME),
        ("file:///etc/passwd", UrlPolicyVerdict.UNSUPPORTED_SCHEME),
        ("javascript:alert(1)", UrlPolicyVerdict.UNSUPPORTED_SCHEME),
        ("data:text/html,<h1>x</h1>", UrlPolicyVerdict.UNSUPPORTED_SCHEME),
    ],
    ids=[
        "empty", "oversized", "space", "tab", "cr", "nl", "backslash",
        "userinfo", "userinfo-no-password", "empty-host-with-path", "no-host",
        "ftp", "file", "javascript", "data",
    ],
)
@pytest.mark.asyncio
async def test_validate_url_rejects_invalid_input(monkeypatch, raw, expected):
    """§1: parser-differential inputs are rejected before any DNS resolution."""
    recorder = []
    _patch_dns(monkeypatch, recorder=recorder)

    verdict, canonical = await validate_url(raw)

    assert verdict is expected
    assert canonical is None
    assert recorder == []


@pytest.mark.parametrize(
    "raw,expected_verdict,expected_canonical",
    [
        ("http://example.com:80", UrlPolicyVerdict.OK, "http://example.com:80"),
        ("https://example.com:443", UrlPolicyVerdict.OK, "https://example.com:443"),
        ("https://example.com", UrlPolicyVerdict.OK, "https://example.com"),
        ("HTTP://example.com/", UrlPolicyVerdict.OK, "http://example.com/"),
        ("http://example.com:22", UrlPolicyVerdict.NON_STANDARD_PORT, None),
        ("http://example.com:6379", UrlPolicyVerdict.NON_STANDARD_PORT, None),
        ("http://example.com:5432", UrlPolicyVerdict.NON_STANDARD_PORT, None),
        ("http://example.com:8080", UrlPolicyVerdict.NON_STANDARD_PORT, None),
    ],
    ids=[
        "port-80", "port-443", "default-port", "canonicalizes-scheme",
        "port-ssh", "port-redis", "port-postgres", "port-alt-http",
    ],
)
@pytest.mark.asyncio
async def test_validate_url_port_and_canonical_policy(
    monkeypatch, raw, expected_verdict, expected_canonical
):
    """§1/§2: only 80/443 allowed; accepted URLs are emitted canonically."""
    verdict, canonical = await validate_url(raw)

    assert verdict is expected_verdict
    assert canonical == expected_canonical


@pytest.mark.parametrize(
    "raw,ips,expected",
    [
        ("http://127.0.0.1/", None, UrlPolicyVerdict.BLOCKED_PRIVATE_IP),
        ("http://10.0.0.1/", None, UrlPolicyVerdict.BLOCKED_PRIVATE_IP),
        ("http://192.168.1.1/", None, UrlPolicyVerdict.BLOCKED_PRIVATE_IP),
        ("http://172.16.1.1/", None, UrlPolicyVerdict.BLOCKED_PRIVATE_IP),
        ("http://[::1]/", None, UrlPolicyVerdict.BLOCKED_PRIVATE_IP),
        ("http://[fc00::1]/", None, UrlPolicyVerdict.BLOCKED_PRIVATE_IP),
        ("http://169.254.169.254/latest/meta-data/", None, UrlPolicyVerdict.BLOCKED_PRIVATE_IP),
        ("http://[::ffff:127.0.0.1]/", None, UrlPolicyVerdict.BLOCKED_PRIVATE_IP),
        (
            "http://multi.test/",
            ("93.184.216.34", "10.0.0.5"),
            UrlPolicyVerdict.BLOCKED_PRIVATE_IP,
        ),
        (
            "http://multi.test/",
            ("10.0.0.5", "93.184.216.34"),
            UrlPolicyVerdict.BLOCKED_PRIVATE_IP,
        ),
        ("http://bad.test/", "fail", UrlPolicyVerdict.BLOCKED_DNS_FAILURE),
    ],
    ids=[
        "loopback", "rfc1918-10", "rfc1918-192", "rfc1918-172",
        "ipv6-loopback", "ipv6-ula-fc00", "cloud-metadata",
        "ipv6-mapped-ipv4", "multi-ip-global-first", "multi-ip-private-first",
        "dns-failure",
    ],
)
@pytest.mark.asyncio
async def test_validate_url_dns_and_ip_policy(monkeypatch, raw, ips, expected):
    """§3: every resolved IP must be global; DNS failure fails closed."""
    _patch_dns(monkeypatch, ips=ips)

    verdict, _ = await validate_url(raw)

    assert verdict is expected


@pytest.mark.parametrize(
    "raw,expected_canonical",
    [
        ("http://example.com/path?q=1", "http://example.com/path?q=1"),
        ("https://example.com/", "https://example.com/"),
        ("https://EXAMPLE.com/../a", "https://EXAMPLE.com/../a"),
        (
            "https://example.com/" + "a" * (web.MAX_URL_LENGTH - 20),
            "https://example.com/" + "a" * (web.MAX_URL_LENGTH - 20),
        ),
    ],
    ids=[
        "query-preserved", "https-slash-root", "host-case-and-dotdot-preserved",
        "length-exactly-max-accepted",
    ],
)
@pytest.mark.asyncio
async def test_validate_url_ok_verdict_returns_parser_canonical_url(raw, expected_canonical):
    verdict, canonical = await validate_url(raw)

    assert verdict is UrlPolicyVerdict.OK
    assert canonical == expected_canonical


@pytest.mark.parametrize(
    "ips",
    [("not-an-ip",), ("93.184.216.34", "999.999.999.999")],
    ids=["garbage-ip-string", "global-then-garbage"],
)
@pytest.mark.asyncio
async def test_validate_url_malformed_dns_sockaddr_returns_invalid_syntax(monkeypatch, ips):
    _patch_dns(monkeypatch, ips=ips)

    verdict, canonical = await validate_url("http://bad.test/")

    assert verdict is UrlPolicyVerdict.INVALID_SYNTAX
    assert canonical is None


@pytest.mark.parametrize(
    "raw",
    ["http://[::1", "http://["],
    ids=["unterminated-ipv6-literal", "empty-ipv6-literal"],
)
@pytest.mark.asyncio
async def test_validate_url_urlparse_exception_maps_to_invalid_syntax(monkeypatch, raw):
    recorder = []
    _patch_dns(monkeypatch, recorder=recorder)

    verdict, canonical = await validate_url(raw)

    assert verdict is UrlPolicyVerdict.INVALID_SYNTAX
    assert canonical is None
    assert recorder == []


@pytest.mark.parametrize(
    "raw",
    ["http://example.com:abc", "http://example.com:99999"],
    ids=["non-numeric-port", "out-of-range-port"],
)
@pytest.mark.asyncio
async def test_validate_url_malformed_port_returns_verdict_not_exception(monkeypatch, raw):
    recorder = []
    _patch_dns(monkeypatch, recorder=recorder)

    verdict, canonical = await validate_url(raw)

    assert verdict is UrlPolicyVerdict.INVALID_SYNTAX
    assert canonical is None
    assert recorder == []


# ============================== web_inspect ==============================


def _handler_single_200(request):
    return httpx.Response(200)


def _handler_multi_hop(request):
    path = request.url.path
    if path == "/a":
        return httpx.Response(301, headers={"location": "/b"})
    if path == "/b":
        return httpx.Response(302, headers={"location": "https://final.test/c"})
    if path == "/c":
        return httpx.Response(200)
    return httpx.Response(404)


@pytest.mark.parametrize(
    "handler,statuses,locations,hop_urls,final_url",
    [
        (
            _handler_single_200, [200], [None],
            ["http://start.test/a"], "http://start.test/a",
        ),
        (
            _handler_multi_hop, [301, 302, 200],
            ["/b", "https://final.test/c", None],
            ["http://start.test/a", "http://start.test/b", "https://final.test/c"],
            "https://final.test/c",
        ),
    ],
    ids=["single-200", "multi-hop-relative"],
)
@pytest.mark.asyncio
async def test_web_inspect_traces_redirects(
    monkeypatch, handler, statuses, locations, hop_urls, final_url
):
    """§12: redirect chains are traced with relative Locations resolved and full provenance."""
    _mock_httpx_client(monkeypatch, handler)

    out = await web_inspect(WebInspectArgs(url="http://start.test/a"))

    assert out.safety_status is UrlPolicyVerdict.OK
    assert [h.status_code for h in out.hops] == statuses
    assert [h.location for h in out.hops] == locations
    assert [h.url for h in out.hops] == hop_urls
    assert out.final_url == final_url
    assert out.final_status == statuses[-1]
    assert out.error is None
    assert out.trust is TrustLevel.DETERMINISTIC
    assert out.provenance is not None
    assert out.provenance.producer_type == "SERVICE"
    assert out.provenance.producer_id == "vouch_web_inspect"
    assert out.provenance.tool_name == "web_inspect"
    assert out.provenance.tool_version == "1.0"
    assert out.provenance.observed_at is not None


@pytest.mark.asyncio
async def test_web_inspect_blocks_initial_url_before_request(monkeypatch):
    """§3: a private initial URL is rejected with zero HTTP round trips."""
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        return httpx.Response(200)

    _mock_httpx_client(monkeypatch, handler)
    url = "http://169.254.169.254/latest/meta-data/"

    out = await web_inspect(WebInspectArgs(url=url))

    assert out.safety_status is UrlPolicyVerdict.BLOCKED_PRIVATE_IP
    assert out.blocked_at == url
    assert out.hops == []
    assert out.error is None
    assert requests_seen == []


@pytest.mark.parametrize(
    "target",
    ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1/admin"],
    ids=["cloud-metadata", "loopback"],
)
@pytest.mark.asyncio
async def test_web_inspect_blocks_redirect_hop_to_private(monkeypatch, target):
    """§3: tracing stops at the hop that points into private space."""
    hosts_requested = []

    def handler(request):
        hosts_requested.append(request.url.host)
        if request.url.host == "start.test":
            return httpx.Response(302, headers={"location": target})
        raise AssertionError("request escaped to a blocked destination")

    _mock_httpx_client(monkeypatch, handler)

    out = await web_inspect(WebInspectArgs(url="http://start.test/x"))

    assert out.safety_status is UrlPolicyVerdict.BLOCKED_PRIVATE_IP
    assert out.blocked_at == target
    assert out.final_url == target
    assert out.error is None
    assert [h.status_code for h in out.hops] == [302]
    assert hosts_requested == ["start.test"]


@pytest.mark.parametrize(
    "scenario", ["max-hops", "connect-timeout"], ids=["max-hops", "connect-timeout"]
)
@pytest.mark.asyncio
async def test_web_inspect_failure_modes(monkeypatch, scenario):
    """Hop exhaustion and transport failures surface as distinct verdicts."""
    if scenario == "max-hops":
        _mock_httpx_client(
            monkeypatch, lambda request: httpx.Response(302, headers={"location": "/loop"})
        )
        out = await web_inspect(WebInspectArgs(url="http://start.test/1"))
        assert out.safety_status is UrlPolicyVerdict.MAX_HOPS_EXCEEDED
        assert len(out.hops) == MAX_REDIRECTS + 1
        assert out.final_status is None
        assert out.final_url == "http://start.test/loop"
    else:
        def timeout_handler(request):
            raise httpx.ConnectTimeout("connect timeout")

        _mock_httpx_client(monkeypatch, timeout_handler)
        out = await web_inspect(WebInspectArgs(url="http://slow.test/"))
        assert out.safety_status is UrlPolicyVerdict.ERROR
        assert out.error == "ConnectTimeout"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.asyncio
async def test_web_inspect_single_redirect_statuses(monkeypatch, status):
    def handler(request):
        if request.url.path == "/a":
            return httpx.Response(status, headers={"location": "/b"})
        if request.url.path == "/b":
            return httpx.Response(200)
        raise AssertionError("unexpected request")

    _mock_httpx_client(monkeypatch, handler)

    out = await web_inspect(WebInspectArgs(url="http://start.test/a"))

    assert out.safety_status is UrlPolicyVerdict.OK
    assert out.final_status == 200
    assert out.final_url == "http://start.test/b"
    assert out.error is None
    assert [(h.url, h.status_code, h.location) for h in out.hops] == [
        ("http://start.test/a", status, "/b"),
        ("http://start.test/b", 200, None),
    ]


@pytest.mark.parametrize(
    "headers",
    [None, {"location": ""}],
    ids=["missing-location", "empty-location"],
)
@pytest.mark.asyncio
async def test_web_inspect_redirect_without_location_is_terminal(monkeypatch, headers):
    _mock_httpx_client(
        monkeypatch, lambda request: httpx.Response(302, headers=headers)
    )

    out = await web_inspect(WebInspectArgs(url="http://start.test/a"))

    assert out.safety_status is UrlPolicyVerdict.OK
    assert out.final_status == 302
    assert out.final_url == "http://start.test/a"
    assert out.error is None
    assert [(h.url, h.status_code, h.location) for h in out.hops] == [
        ("http://start.test/a", 302, None),
    ]


@pytest.mark.parametrize(
    "status,headers",
    [(300, {"location": "/next"}), (304, None)],
    ids=["300-with-location", "304-not-modified"],
)
@pytest.mark.asyncio
async def test_web_inspect_non_redirect_3xx_is_terminal(monkeypatch, status, headers):
    _mock_httpx_client(
        monkeypatch, lambda request: httpx.Response(status, headers=headers)
    )

    out = await web_inspect(WebInspectArgs(url="http://start.test/a"))

    assert out.safety_status is UrlPolicyVerdict.OK
    assert out.final_status == status
    assert out.final_url == "http://start.test/a"
    assert out.error is None
    assert len(out.hops) == 1
    assert out.hops[0].location == (headers or {}).get("location")


@pytest.mark.parametrize(
    "status",
    [403, 404, 500],
    ids=["forbidden", "not-found", "server-error"],
)
@pytest.mark.asyncio
async def test_web_inspect_http_error_statuses_are_terminal(monkeypatch, status):
    _mock_httpx_client(monkeypatch, lambda request: httpx.Response(status))

    out = await web_inspect(WebInspectArgs(url="http://start.test/a"))

    assert out.safety_status is UrlPolicyVerdict.OK
    assert out.final_status == status
    assert out.final_url == "http://start.test/a"
    assert out.error is None
    assert len(out.hops) == 1
    assert out.trust is TrustLevel.DETERMINISTIC
    assert out.provenance is not None
    assert out.provenance.producer_id == "vouch_web_inspect"
    assert out.provenance.tool_name == "web_inspect"
    assert out.provenance.tool_version == "1.0"


@pytest.mark.parametrize(
    "url,verdict,ips",
    [
        ("http://start.test:8080/", UrlPolicyVerdict.NON_STANDARD_PORT, None),
        ("ftp://x.test/file", UrlPolicyVerdict.UNSUPPORTED_SCHEME, None),
        ("http://user:pass@x.test/", UrlPolicyVerdict.INVALID_SYNTAX, None),
        ("http://start.test/" + "a" * 3000, UrlPolicyVerdict.INVALID_SYNTAX, None),
        ("http://x.test/", UrlPolicyVerdict.BLOCKED_DNS_FAILURE, "fail"),
    ],
    ids=[
        "non-standard-port", "unsupported-scheme", "userinfo-syntax",
        "oversized-truncated", "blocked-dns-failure",
    ],
)
@pytest.mark.asyncio
async def test_web_inspect_initial_url_policy_rejections(
    monkeypatch, url, verdict, ips
):
    if ips is not None:
        _patch_dns(monkeypatch, ips=ips)
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        return httpx.Response(200)

    _mock_httpx_client(monkeypatch, handler)

    out = await web_inspect(WebInspectArgs(url=url))

    assert out.safety_status is verdict
    assert out.error == verdict.value
    assert out.blocked_at is None
    assert out.hops == []
    assert out.final_status is None
    assert out.initial_url == url[: web.MAX_URL_LENGTH]
    assert out.final_url == url[: web.MAX_URL_LENGTH]
    assert requests_seen == []


@pytest.mark.asyncio
async def test_web_inspect_truncates_long_location(monkeypatch):
    long_location = "/next/" + "x" * 3000

    def handler(request):
        if request.url.path == "/a":
            return httpx.Response(302, headers={"location": long_location})
        raise AssertionError("oversized redirect target must not be requested")

    _mock_httpx_client(monkeypatch, handler)

    out = await web_inspect(WebInspectArgs(url="http://start.test/a"))

    assert out.safety_status is UrlPolicyVerdict.INVALID_SYNTAX
    assert out.error == "invalid_syntax"
    assert out.blocked_at is None
    assert len(out.hops) == 1
    assert out.hops[0].location == long_location[: web.MAX_URL_LENGTH]
    assert out.final_url.startswith("http://start.test/next/")
    assert len(out.final_url) == web.MAX_URL_LENGTH


@pytest.mark.asyncio
async def test_web_inspect_max_hops_final_url_truncated(monkeypatch):
    count = 0
    long_location = "/long/" + "x" * 3000

    def handler(request):
        nonlocal count
        count += 1
        if count == MAX_REDIRECTS + 1:
            return httpx.Response(301, headers={"location": long_location})
        return httpx.Response(301, headers={"location": "/loop"})

    _mock_httpx_client(monkeypatch, handler)

    out = await web_inspect(WebInspectArgs(url="http://start.test/1"))

    assert out.safety_status is UrlPolicyVerdict.MAX_HOPS_EXCEEDED
    assert out.final_status is None
    assert len(out.hops) == MAX_REDIRECTS + 1
    assert out.hops[-1].location == long_location[: web.MAX_URL_LENGTH]
    assert out.final_url.startswith("http://start.test/long/")
    assert len(out.final_url) == web.MAX_URL_LENGTH


@pytest.mark.parametrize(
    "exc,expected_error",
    [
        (httpx.HTTPError("boom"), "HTTPError"),
        (httpx.InvalidURL("bad url"), "InvalidURL"),
        (ValueError("bad value"), "ValueError"),
        (TimeoutError(), "TimeoutError"),
    ],
    ids=["http-error", "invalid-url", "value-error", "timeout-error"],
)
@pytest.mark.asyncio
async def test_web_inspect_transport_error_mapping(monkeypatch, exc, expected_error):
    def handler(request):
        raise exc

    _mock_httpx_client(monkeypatch, handler)

    out = await web_inspect(WebInspectArgs(url="http://start.test/"))

    assert out.safety_status is UrlPolicyVerdict.ERROR
    assert out.error == expected_error
    assert out.initial_url == "http://start.test/"
    assert out.final_url == "http://start.test/"
    assert out.hops == []


@pytest.mark.asyncio
async def test_web_inspect_error_preserves_hops(monkeypatch):
    def handler(request):
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        raise httpx.ReadTimeout("slow")

    _mock_httpx_client(monkeypatch, handler)

    out = await web_inspect(WebInspectArgs(url="http://start.test/a"))

    assert out.safety_status is UrlPolicyVerdict.ERROR
    assert out.error == "ReadTimeout"
    assert out.initial_url == "http://start.test/a"
    assert out.final_url == "http://start.test/b"
    assert [(h.url, h.status_code) for h in out.hops] == [
        ("http://start.test/a", 301)
    ]


@pytest.mark.asyncio
async def test_web_inspect_request_timeout_at_transport_boundary(monkeypatch):
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200)

    _mock_httpx_client(monkeypatch, handler)

    out = await web_inspect(WebInspectArgs(url="http://start.test/"))

    assert out.final_status == 200
    assert seen["timeout"] == {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}


@pytest.mark.asyncio
async def test_web_inspect_ignores_env_proxy(monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    pools = []

    async def fake_handle(self, request, *args):
        pools.append(type(self._pool).__name__)
        return await transport.handle_async_request(request, *args)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_handle)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:9999")

    out = await web_inspect(WebInspectArgs(url="http://start.test/"))

    assert out.final_status == 200
    assert pools == ["AsyncConnectionPool"]


# ============================== extract_links_from_markdown ==============================


@pytest.mark.parametrize(
    "content,base,expected",
    [
        (
            "[Download](/download.apk)\n[Verify](../verify)",
            "https://site.test/posts/2026/item",
            [
                ("https://site.test/download.apk", "Download"),
                ("https://site.test/posts/verify", "Verify"),
            ],
        ),
        (
            "[Login](https://site.com/login#form)",
            "https://site.com/",
            [("https://site.com/login", "Login")],
        ),
        (
            "[Click](javascript:void(0))\n[Top](#top)\njavascript:void(0)\n"
            "[Real](https://site.com/ok)",
            "https://site.com/",
            [("https://site.com/ok", "Real")],
        ),
        ("see https://site.test/x now", "https://site.test/", [("https://site.test/x", None)]),
        (
            "\n".join(f"[Link {i}](https://site.test/p{i})" for i in range(500)),
            "https://site.test/",
            [(f"https://site.test/p{i}", f"Link {i}") for i in range(50)],
        ),
        (
            "[Rel](/docs?q=1#frag)",
            "https://site.test/x",
            [("https://site.test/docs?q=1", "Rel")],
        ),
        ("[Mail](mailto:a@b.test) [Call](tel:+123)", "https://site.test/", []),
        (
            "[rel](nextpage)",
            "https://site.test/" + "b" * 2040 + "/dir",
            [(("https://site.test/" + "b" * 2040 + "/nextpage")[: web.MAX_URL_LENGTH], "rel")],
        ),
    ],
    ids=[
        "relative",
        "fragment-stripped",
        "javascript-and-anchors-dropped",
        "bare-raw-url",
        "cap-at-50",
        "relative-query-fragment",
        "mailto-tel-dropped",
        "url-cap-truncated",
    ],
)
def test_extract_links_normalizes_filters_and_caps(content, base, expected):
    """Link derivation normalizes targets, drops unsafe schemes, and caps output."""
    links = extract_links_from_markdown(content, base)

    assert [(link.url, link.text) for link in links] == expected
    for link in links:
        assert link.derived_from == base
        assert link.trust is TrustLevel.UNTRUSTED_ARTIFACT
        assert link.provenance is not None
        assert link.provenance.producer_type == "SERVICE"
        assert link.provenance.producer_id == "obscura"
        assert link.provenance.tool_name == "extract_page_content"
        assert link.provenance.tool_version == "1.0"
        assert link.provenance.observed_at is not None


@pytest.mark.parametrize(
    "content",
    [
        ("[" * 50_000),
        ("x" * 100_000 + "[Evil](http://evil.test/past)"),
        ("x" * 100_000 + "http://evil.test/past"),
    ],
    ids=["pathological-brackets", "markdown-past-pre-slice", "raw-url-past-pre-slice"],
)
def test_extract_links_bounds_input(content):
    """§7: pathological input stays fast and content past 100k is never scanned."""
    start = time.perf_counter()
    links = extract_links_from_markdown(content, "https://site.test/page")
    elapsed = time.perf_counter() - start

    assert links == []
    assert elapsed < REDOS_TIME_BUDGET_SECS


def test_extract_links_deduplicates_across_forms():
    """The same normalized URL appears once across markdown and raw forms."""
    content = "[A](https://a.test/x) also https://a.test/x and [B](https://a.test/x)"

    links = extract_links_from_markdown(content, "https://site.test/")

    assert [(link.url, link.text) for link in links] == [("https://a.test/x", "A")]


@pytest.mark.parametrize(
    "content,expected",
    [
        ("[A](https://a.test/x.)", [("https://a.test/x", "A")]),
        ("see https://a.test/x.,;:!? end", [("https://a.test/x", None)]),
        ("(https://a.test/x).,;:!?)]}'", [("https://a.test/x", None)]),
        ("[A](https://a.test/x).,;:!?)]}'", [("https://a.test/x", "A")]),
    ],
    ids=["markdown-target", "raw-attached", "paren-wrapped", "after-closing-paren"],
)
def test_extract_links_strips_trailing_punctuation(content, expected):
    """Configured trailing characters are stripped from the target before storage."""
    links = extract_links_from_markdown(content, "https://site.test/")

    assert [(link.url, link.text) for link in links] == expected


def test_extract_links_raw_url_cap_at_50():
    """The raw-URL loop breaks at the shared 50-link cap."""
    content = " ".join(f"https://r.test/p{i}" for i in range(100))

    links = extract_links_from_markdown(content, "https://site.test/")

    assert [(link.url, link.text) for link in links] == [
        (f"https://r.test/p{i}", None) for i in range(50)
    ]


@pytest.mark.parametrize(
    "content",
    ["[F](ftp://files.test/x)", "[B](http:/path)"],
    ids=["ftp-scheme", "empty-netloc"],
)
def test_extract_links_non_http_targets_skipped(content):
    """Targets failing the scheme or netloc check after urljoin are dropped."""
    assert extract_links_from_markdown(content, "https://site.test/x") == []


@pytest.mark.parametrize(
    "content,expected",
    [
        ("[" + "x" * 300 + "](https://a.test/p)", [("https://a.test/p", None)]),
        (
            "[ok](https://a.test/" + "p" * 3000 + ")",
            [(("https://a.test/" + "p" * 3000)[: web.MAX_URL_LENGTH], None)],
        ),
        (
            "\x00\x01\xff garbage ]( [unbalanced ( https://ok.test/in ",
            [("https://ok.test/in", None)],
        ),
    ],
    ids=["oversized-anchor", "oversized-url", "garbage-binary"],
)
def test_extract_links_odd_input_best_effort(content, expected):
    """Bounded regexes keep odd input fast and return a best-effort list."""
    start = time.perf_counter()
    links = extract_links_from_markdown(content, "https://site.test/")
    elapsed = time.perf_counter() - start

    assert [(link.url, link.text) for link in links] == expected
    assert elapsed < REDOS_TIME_BUDGET_SECS


# ============================== extract_page_content ==============================


@pytest.mark.parametrize(
    "case",
    [
        (
            LONG_PAGE_BODY + b"\n[Go](/rel)",
            b'Page loaded: https://final.test/land - "Daily Report"',
            "https://final.test/land",
            "Daily Report",
            ["https://final.test/rel"],
            False,
            "Body text",
        ),
        (b"<div id='app'></div>", b"", "http://start.test/page", None, [], True, "<div"),
        (
            "Welcome \u202Eevil.txt\u200B to the site, plenty of visible text here.".encode(),
            b"",
            "http://start.test/page",
            None,
            [],
            False,
            "Welcome",
        ),
        (b"[Go](https://a.test/x)", b"", "http://start.test/page", None, ["https://a.test/x"], True, "[Go]"),
        (b"y" * 29, b"", "http://start.test/page", None, [], True, "y"),
        (b"y" * 30, b"", "http://start.test/page", None, [], False, "y"),
    ],
    ids=[
        "full-page",
        "spa-shell",
        "bidi-stripped",
        "short-with-link",
        "boundary-29",
        "boundary-30",
    ],
)
@pytest.mark.asyncio
async def test_extract_success_output(monkeypatch, case):
    """§8/§10/§12: parsed output is sanitized, re-validated, and fully tagged."""
    stdout, stderr, expected_final, expected_title, expected_links, expected_empty, visible = case
    _fake_obscura(monkeypatch, stdout=stdout, stderr=stderr)

    out = await extract_page_content(ExtractPageContentArgs(url="http://start.test/page"))

    assert out.error is None
    assert out.url == "http://start.test/page"
    assert out.final_url == expected_final
    assert out.page_title == expected_title
    assert [link.url for link in out.links] == expected_links
    assert out.is_empty_render is expected_empty
    assert visible in out.markdown_content
    assert not any(unicodedata.category(c) in ("Cf", "Cs") for c in out.markdown_content)
    assert out.trust is TrustLevel.UNTRUSTED_ARTIFACT
    assert out.provenance is not None
    assert out.provenance.producer_type == "SERVICE"
    assert out.provenance.producer_id == "obscura"
    assert out.provenance.tool_name == "extract_page_content"
    assert out.provenance.tool_version == "1.0"
    assert out.provenance.observed_at is not None


@pytest.mark.parametrize(
    "private_url",
    ["http://127.0.0.1/steal", "http://169.254.169.254/steal"],
    ids=["loopback", "cloud-metadata"],
)
@pytest.mark.asyncio
async def test_extract_blocks_post_redirect_final_url(monkeypatch, private_url):
    """§10: content fetched from a private landing URL is rejected, not returned."""
    _fake_obscura(
        monkeypatch,
        stdout=LONG_PAGE_BODY,
        stderr=f'Page loaded: {private_url} - "x"'.encode(),
    )

    out = await extract_page_content(ExtractPageContentArgs(url="http://start.test/page"))

    assert out.error == "redirect_blocked_blocked_private_ip"
    assert out.final_url is None
    assert out.markdown_content is None
    assert out.links == []


@pytest.mark.parametrize(
    "scenario,rc",
    [("rc-failure", 1), ("rc-failure", 2), ("timeout", None)],
    ids=["obscura-rc-1", "obscura-rc-2", "timeout"],
)
@pytest.mark.asyncio
async def test_extract_failure_modes(monkeypatch, caplog, scenario, rc):
    """Obscura failures and timeouts map to stable error outputs."""
    if scenario == "rc-failure":
        _fake_obscura(monkeypatch, returncode=rc)
        with caplog.at_level(logging.WARNING):
            out = await extract_page_content(ExtractPageContentArgs(url="http://start.test/page"))
        assert out.error == f"obscura_failed_{rc}"
        assert out.markdown_content is None
        assert any(f"(rc={rc})" in message for message in caplog.messages)
    else:
        monkeypatch.setattr(web, "_run_obscura_safe", AsyncMock(side_effect=TimeoutError))
        out = await extract_page_content(ExtractPageContentArgs(url="http://start.test/page"))
        assert out.error == "timeout"
        assert out.markdown_content is None


@pytest.mark.parametrize("exc_type", [ValueError, OSError], ids=["value-error", "os-error"])
@pytest.mark.asyncio
async def test_extract_unexpected_exception_maps_type_name(monkeypatch, caplog, exc_type):
    """Any other exception maps to its type name and is logged with traceback."""
    monkeypatch.setattr(web, "_run_obscura_safe", AsyncMock(side_effect=exc_type("boom")))

    with caplog.at_level(logging.ERROR):
        out = await extract_page_content(ExtractPageContentArgs(url="http://start.test/page"))

    assert out.error == exc_type.__name__
    assert out.markdown_content is None
    assert out.links == []
    assert any("extract_page_content error" in message for message in caplog.messages)
    assert any(record.exc_info is not None for record in caplog.records)


@pytest.mark.asyncio
async def test_extract_calls_obscura_with_canonical_args(monkeypatch):
    """Obscura gets fixed argv, the canonical URL, and a 20s timeout."""
    spy = AsyncMock(
        return_value=(0, "a body with enough visible characters here", "", "http://start.test/page", None)
    )
    monkeypatch.setattr(web, "_run_obscura_safe", spy)

    out = await extract_page_content(ExtractPageContentArgs(url="HTTP://start.test/page"))

    assert out.error is None
    assert out.url == "HTTP://start.test/page"
    assert out.final_url == "http://start.test/page"
    assert spy.await_args.args[0] == ["--dump", "markdown"]
    assert spy.await_args.args[1] == "http://start.test/page"
    assert spy.await_args.kwargs == {"timeout_secs": 20.0}


# ============================== pre-flight gate (both tools) ==============================


@pytest.mark.parametrize(
    "tool,args,expected_error",
    [
        (extract_page_content, ExtractPageContentArgs(url="http://127.0.0.1/"), "blocked_private_ip"),
        (
            extract_page_content,
            ExtractPageContentArgs(url="http://169.254.169.254/latest/meta-data/"),
            "blocked_private_ip",
        ),
        (screenshot_page, ScreenshotPageArgs(url="http://127.0.0.1/"), "blocked_private_ip"),
        (
            screenshot_page,
            ScreenshotPageArgs(url="http://169.254.169.254/latest/meta-data/"),
            "blocked_private_ip",
        ),
        (extract_page_content, ExtractPageContentArgs(url="ftp://files.test/x"), "unsupported_scheme"),
        (
            extract_page_content,
            ExtractPageContentArgs(url="http://start.test:8080/x"),
            "non_standard_port",
        ),
        (
            extract_page_content,
            ExtractPageContentArgs(url="http://user@start.test/"),
            "invalid_syntax",
        ),
    ],
    ids=[
        "extract-loopback",
        "extract-metadata",
        "screenshot-loopback",
        "screenshot-metadata",
        "extract-unsupported-scheme",
        "extract-non-standard-port",
        "extract-invalid-syntax",
    ],
)
@pytest.mark.asyncio
async def test_blocks_private_url_before_spawn(monkeypatch, tool, args, expected_error):
    """§3: a rejected initial URL never reaches the Obscura subprocess."""
    obscura = AsyncMock()
    monkeypatch.setattr(web, "_run_obscura_safe", obscura)

    out = await tool(args)

    assert out.error == expected_error
    assert out.url == args.url
    obscura.assert_not_awaited()


# ============================== screenshot_page ==============================


@pytest.mark.parametrize(
    "width,height", [(1280, 720), (320, 240)], ids=["hd", "small"]
)
@pytest.mark.asyncio
async def test_screenshot_success_content_addressed(monkeypatch, tmp_path, width, height):
    """§11/§12: a valid capture lands at <sha256>.png behind an opaque handle."""
    _patch_artifacts_dir(monkeypatch, tmp_path)
    tempdirs = _record_tempdirs(monkeypatch)
    data = _png_bytes(width, height)
    monkeypatch.setattr(web, "_run_obscura_safe", _obscura_writes_capture(data))
    digest = hashlib.sha256(data).hexdigest()

    out = await screenshot_page(ScreenshotPageArgs(url="https://site.test/page"))

    assert out.error is None
    assert out.width == width
    assert out.height == height
    assert out.file_size_bytes == len(data)
    assert out.sha256_digest == digest
    stored = tmp_path / "artifacts" / f"{digest}.png"
    assert stored.read_bytes() == data
    assert out.artifact_handle == f"art_{digest[:16]}"
    assert len(out.artifact_handle) == 20
    assert out.artifact_id
    assert uuid.UUID(out.artifact_id).version == 7
    for value in out.model_dump().values():
        if isinstance(value, str):
            assert not value.startswith("/")
            assert str(tmp_path) not in value
    assert str(tmp_path) not in out.model_dump_json()
    assert out.trust is TrustLevel.UNTRUSTED_ARTIFACT
    assert out.provenance is not None
    assert out.provenance.producer_type == "SERVICE"
    assert out.provenance.producer_id == "obscura"
    assert out.provenance.tool_name == "screenshot_page"
    assert out.provenance.tool_version == "1.0"
    assert out.provenance.observed_at is not None
    assert out.provenance.artifact_sha256 == digest
    assert tempdirs
    for path in tempdirs:
        assert Path(path).name.startswith("vouch_scr_")
        assert not Path(path).exists()


@pytest.mark.parametrize(
    "png_bytes,returncode,expected_error",
    [
        (
            b"NOTAPNG!" + b"\x00" * 8 + struct.pack(">II", 1280, 720) + b"\x00" * 8,
            0,
            "invalid_png_data",
        ),
        (b"\x89PNG", 0, "invalid_png_data"),
        (b"", 0, "invalid_png_data"),
        (b"\x89PNG\r\n\x1a\n", 0, "invalid_png_data"),
        (_png_bytes(0, 720), 0, "invalid_png_data"),
        (_png_bytes(1280, 0), 0, "invalid_png_data"),
        (b"", 1, "obscura_failed_1"),
    ],
    ids=[
        "wrong-magic", "truncated", "zero-byte", "header-only",
        "zero-width", "zero-height", "obscura-rc-failure",
    ],
)
@pytest.mark.asyncio
async def test_screenshot_rejects_invalid_capture_and_cleans_up(
    monkeypatch, tmp_path, caplog, png_bytes, returncode, expected_error
):
    """§11: corrupt or failed captures never reach storage and leave no scratch dirs."""
    artifacts_dir = _patch_artifacts_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        web, "_run_obscura_safe", _obscura_writes_capture(png_bytes, returncode=returncode)
    )
    tempdirs = _record_tempdirs(monkeypatch)

    with caplog.at_level(logging.WARNING):
        out = await screenshot_page(ScreenshotPageArgs(url="https://site.test/page"))

    assert out.error == expected_error
    assert out.artifact_id is None
    assert list(artifacts_dir.iterdir()) == []
    assert tempdirs
    for path in tempdirs:
        assert Path(path).name.startswith("vouch_scr_")
        assert not Path(path).exists()
    if expected_error.startswith("obscura_failed"):
        assert any(f"(rc={returncode})" in message for message in caplog.messages)


@pytest.mark.parametrize(
    "final_url,png_bytes",
    [
        ("http://127.0.0.1/steal", _png_bytes(800, 600)),
        ("http://169.254.169.254/steal", _png_bytes(800, 600)),
        ("http://127.0.0.1/steal", b"NOTAPNG!"),
    ],
    ids=["loopback", "cloud-metadata", "invalid-png-order"],
)
@pytest.mark.asyncio
async def test_screenshot_blocks_post_redirect_final_url(
    monkeypatch, tmp_path, final_url, png_bytes
):
    """§10: landing-URL policy is checked before the capture is read or staged."""
    artifacts_dir = _patch_artifacts_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        web,
        "_run_obscura_safe",
        _obscura_writes_capture(png_bytes, final_url=final_url),
    )

    out = await screenshot_page(ScreenshotPageArgs(url="https://site.test/page"))

    assert out.error == "redirect_blocked_blocked_private_ip"
    assert out.artifact_id is None
    assert list(artifacts_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_screenshot_existing_artifact_not_overwritten(monkeypatch, tmp_path):
    """An existing content-addressed file is left untouched; metadata still returned."""
    artifacts_dir = _patch_artifacts_dir(monkeypatch, tmp_path)
    data = _png_bytes(640, 480)
    digest = hashlib.sha256(data).hexdigest()
    sentinel = b"SENTINEL-DO-NOT-OVERWRITE"
    (artifacts_dir / f"{digest}.png").write_bytes(sentinel)
    monkeypatch.setattr(web, "_run_obscura_safe", _obscura_writes_capture(data))

    out = await screenshot_page(ScreenshotPageArgs(url="https://site.test/page"))

    assert out.error is None
    assert (artifacts_dir / f"{digest}.png").read_bytes() == sentinel
    assert out.sha256_digest == digest
    assert out.artifact_handle == f"art_{digest[:16]}"
    assert out.file_size_bytes == len(data)
    assert (out.width, out.height) == (640, 480)


@pytest.mark.asyncio
async def test_screenshot_missing_capture_logs_warning(monkeypatch, tmp_path, caplog):
    """rc=0 without a written file still maps to obscura_failed_0 and warns."""
    artifacts_dir = _patch_artifacts_dir(monkeypatch, tmp_path)

    async def no_write(cmd_args, target_url, timeout_secs=20.0):
        return (0, "", "", target_url, None)

    monkeypatch.setattr(web, "_run_obscura_safe", no_write)

    with caplog.at_level(logging.WARNING):
        out = await screenshot_page(ScreenshotPageArgs(url="https://site.test/page"))

    assert out.error == "obscura_failed_0"
    assert out.artifact_id is None
    assert list(artifacts_dir.iterdir()) == []
    assert any("(rc=0)" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_screenshot_timeout_maps_to_error(monkeypatch, tmp_path):
    """A TimeoutError from Obscura maps to timeout and still cleans its scratch dir."""
    _patch_artifacts_dir(monkeypatch, tmp_path)
    tempdirs = _record_tempdirs(monkeypatch)
    monkeypatch.setattr(web, "_run_obscura_safe", AsyncMock(side_effect=TimeoutError))

    out = await screenshot_page(ScreenshotPageArgs(url="https://site.test/page"))

    assert out.error == "timeout"
    assert out.artifact_id is None
    assert tempdirs
    for path in tempdirs:
        assert not Path(path).exists()


@pytest.mark.parametrize("exc_type", [ValueError, OSError], ids=["value-error", "os-error"])
@pytest.mark.asyncio
async def test_screenshot_unexpected_exception_maps_type_name(
    monkeypatch, tmp_path, caplog, exc_type
):
    """Any other exception maps to its type name and is logged with traceback."""
    _patch_artifacts_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(web, "_run_obscura_safe", AsyncMock(side_effect=exc_type("boom")))

    with caplog.at_level(logging.ERROR):
        out = await screenshot_page(ScreenshotPageArgs(url="https://site.test/page"))

    assert out.error == exc_type.__name__
    assert out.artifact_id is None
    assert any("screenshot_page error" in message for message in caplog.messages)
    assert any(record.exc_info is not None for record in caplog.records)


@pytest.mark.asyncio
async def test_screenshot_calls_obscura_with_capture_args(monkeypatch, tmp_path):
    """Obscura gets -s into a vouch_scr_ dir, the canonical URL, and a 20s timeout."""
    artifacts_dir = _patch_artifacts_dir(monkeypatch, tmp_path)
    data = _png_bytes(640, 480)
    seen = {}

    async def fake(cmd_args, target_url, timeout_secs=20.0):
        seen.update(cmd_args=cmd_args, target_url=target_url, timeout_secs=timeout_secs)
        Path(cmd_args[cmd_args.index("-s") + 1]).write_bytes(data)
        return (0, "", "", target_url, None)

    monkeypatch.setattr(web, "_run_obscura_safe", fake)

    out = await screenshot_page(ScreenshotPageArgs(url="HTTP://start.test/page"))

    assert out.error is None
    assert out.url == "HTTP://start.test/page"
    assert seen["cmd_args"][0] == "-s"
    capture_path = Path(seen["cmd_args"][1])
    assert capture_path.name == "capture.png"
    assert capture_path.parent.name.startswith("vouch_scr_")
    assert not capture_path.parent.exists()
    assert seen["target_url"] == "http://start.test/page"
    assert seen["timeout_secs"] == 20.0
    assert list(artifacts_dir.iterdir()) != []


# ============================== web_search ==============================


@pytest.mark.asyncio
async def test_web_search_success(monkeypatch):
    """A clean payload maps to truncated results with external-source provenance."""
    _patch_tavily(
        monkeypatch,
        response={
            "results": [
                _search_item(title="T" * 400, content="c" * 2500),
                _search_item(
                    title="Second",
                    url="http://site.test/two",
                    content="second snippet",
                    score=0.5,
                    published_date="2024-05-02T10:00:00Z",
                ),
            ]
        },
    )

    out = await web_search(WebSearchArgs(query="query text"))

    assert out.error is None
    assert len(out.results) == 2
    first, second = out.results
    assert first.title == "T" * 300
    assert first.snippet == "c" * 2000
    assert first.score == 0.9
    assert first.published_date == datetime(2024, 5, 1)
    assert second.title == "Second"
    assert second.url == "http://site.test/two"
    assert second.snippet == "second snippet"
    assert second.score == 0.5
    assert second.published_date == datetime(2024, 5, 2, 10, 0, tzinfo=timezone.utc)
    assert out.trust is TrustLevel.EXTERNAL_SOURCE
    assert out.provenance is not None
    assert out.provenance.producer_type == "SERVICE"
    assert out.provenance.producer_id == "tavily"
    assert out.provenance.tool_name == "web_search"
    assert out.provenance.tool_version == "1.0"
    assert out.provenance.artifact_sha256 is None
    assert out.provenance.observed_at is not None


@pytest.mark.asyncio
async def test_web_search_calls_provider_with_contract(monkeypatch):
    """The provider receives the fixed search contract."""
    search = _patch_tavily(monkeypatch, response={"results": []})

    await web_search(WebSearchArgs(query="contract query"))

    search.assert_awaited_once_with(
        query="contract query",
        timeout=12.0,
        search_depth="basic",
        topic="general",
        max_results=10,
        include_answer=False,
        include_raw_content=False,
        include_images=False,
    )


@pytest.mark.parametrize(
    "bad_item",
    [
        "not-a-dict",
        _search_item(url="javascript:alert(1)"),
        _search_item(url="ftp://site.test/file"),
        _search_item(url="not a url"),
        _search_item(url="http://user:pass@host"),
        _search_item(url="http://user@host"),
        _search_item(title="", content=""),
        _search_item(title=123, content=456),
        _search_item(score="high"),
        _search_item(published_date="notadate"),
    ],
    ids=[
        "non-dict",
        "javascript-scheme",
        "ftp-scheme",
        "no-host",
        "userinfo-password",
        "userinfo-username",
        "empty-title-snippet",
        "non-str-title-content",
        "score-wrong-type",
        "published-date-wrong-type",
    ],
)
@pytest.mark.asyncio
async def test_web_search_filters_invalid_items(monkeypatch, bad_item):
    """Dirty items are dropped individually; the call still succeeds."""
    _patch_tavily(monkeypatch, response={"results": [_search_item(), bad_item]})

    out = await web_search(WebSearchArgs(query="query text"))

    assert out.error is None
    assert [r.url for r in out.results] == ["http://site.test/page"]


@pytest.mark.asyncio
async def test_web_search_caps_raw_results(monkeypatch):
    """Only the first 10 provider items are considered."""
    items = [_search_item(title=f"t{i}", url=f"http://site.test/{i}") for i in range(12)]
    _patch_tavily(monkeypatch, response={"results": items})

    out = await web_search(WebSearchArgs(query="query text"))

    assert out.error is None
    assert [r.title for r in out.results] == [f"t{i}" for i in range(10)]


@pytest.mark.parametrize(
    "length,valid",
    [(2, False), (3, True), (500, True), (501, False)],
    ids=["too-short-2", "min-3", "max-500", "too-long-501"],
)
def test_web_search_query_length_boundaries(length, valid):
    """Args validation accepts 3..500 chars and rejects outside that range."""
    if valid:
        assert WebSearchArgs(query="a" * length).query == "a" * length
    else:
        with pytest.raises(ValidationError):
            WebSearchArgs(query="a" * length)


_REQUEST = httpx.Request("GET", "https://api.tavily.com/search")
_RESPONSE_429 = httpx.Response(429, request=_REQUEST)


@pytest.mark.parametrize(
    "exc,expected_error,log_fragment",
    [
        (UsageLimitExceededError("msg"), WebSearchError.USAGE_LIMIT_EXCEEDED, "usage limit reached"),
        (InvalidAPIKeyError("msg"), WebSearchError.AUTHENTICATION_FAILED, None),
        (MissingAPIKeyError(), WebSearchError.AUTHENTICATION_FAILED, None),
        (ForbiddenError("msg"), WebSearchError.ACCESS_DENIED, None),
        (BadRequestError("msg"), WebSearchError.BAD_REQUEST, None),
        (httpx.RequestError("msg", request=_REQUEST), WebSearchError.NETWORK_ERROR, None),
        (
            httpx.HTTPStatusError("msg", request=_REQUEST, response=_RESPONSE_429),
            WebSearchError.HTTP_ERROR,
            "status=429",
        ),
        (RuntimeError("boom"), WebSearchError.INTERNAL_ERROR, None),
    ],
    ids=[
        "usage-limit",
        "invalid-key",
        "missing-key",
        "forbidden",
        "bad-request",
        "request-error",
        "status-error",
        "unexpected",
    ],
)
@pytest.mark.asyncio
async def test_web_search_error_mapping(
    monkeypatch, caplog, exc, expected_error, log_fragment
):
    """Each provider failure maps to its error enum with failure provenance."""
    _patch_tavily(monkeypatch, side_effect=exc)

    with caplog.at_level(logging.DEBUG):
        out = await web_search(WebSearchArgs(query="query text"))

    assert out.error == expected_error
    assert out.results == []
    assert out.provenance is not None
    assert out.provenance.producer_id == "tavily"
    assert out.provenance.tool_name == "web_search"
    assert out.provenance.artifact_sha256 is None
    if log_fragment:
        assert any(log_fragment in message for message in caplog.messages)


# ============================== _run_obscura_safe ==============================


@pytest.fixture(scope="module")
def obscura_server():
    """Local server: small page with format chars, big ASCII page, hanging route."""
    small = (
        '<!doctype html><html><head><title>Daily Report</title></head>'
        "<body><h1>Report</h1><p>Visible \u202ereversed\u202c and zero\u200bwidth text.</p>"
        "</body></html>"
    ).encode()
    big = (
        "<!doctype html><html><head><title>Big</title></head><body><h1>Report</h1><p>"
        + "lorem ipsum dolor sit amet " * 25000
        + "</p></body></html>"
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/hang":
                time.sleep(300)
                return
            body = big if self.path == "/big" else small
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def real_obscura(monkeypatch):
    """Resolve a real Obscura binary; skip the test when none is available."""
    path = shutil.which("obscura")
    if path is None:
        try:
            path = ensure_obscura()
        except Exception as e:
            pytest.skip(f"obscura binary not available: {e}")
    resolved = Path(path)
    monkeypatch.setattr(web.settings, "obscura_path", resolved)
    return resolved


@pytest.fixture
def procfs():
    if not Path("/proc").is_dir():
        pytest.skip("requires procfs (Linux)")
    return Path("/proc")


def _obscura_pids():
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parts = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if parts and parts[0].endswith(b"obscura") and b"fetch" in parts:
            pids.append(int(entry.name))
    return pids


async def _wait_obscura_proc(procfs, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for pid in _obscura_pids():
            try:
                parts = (procfs / str(pid) / "cmdline").read_bytes().split(b"\0")
                env = (procfs / str(pid) / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            argv = [p.decode() for p in parts if p]
            env_keys = sorted(e.split(b"=", 1)[0].decode() for e in env if e)
            return pid, argv, env_keys
        await asyncio.sleep(0.05)
    raise AssertionError("obscura process never appeared")


async def _assert_obscura_gone(timeout=2.0):
    deadline = time.monotonic() + timeout
    while _obscura_pids() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _obscura_pids(), f"obscura still running: {_obscura_pids()}"


@pytest.mark.asyncio
async def test_run_obscura_parses_page_loaded_and_strips_format_chars(
    real_obscura, obscura_server
):
    """Real fetch parses the page-loaded line and strips Cf/Cs chars from stdout."""
    target = f"{obscura_server}/small"
    rc, content, stderr, final_url, title = await web._run_obscura_safe(
        ["--allow-private-network", "--dump", "markdown"], target
    )

    assert rc == 0
    assert final_url == target
    assert title == "Daily Report"
    assert "Report" in content
    assert "Page loaded:" in stderr
    assert not any(unicodedata.category(c) in ("Cf", "Cs") for c in content)


@pytest.mark.asyncio
async def test_run_obscura_without_page_loaded_falls_back(real_obscura, obscura_server):
    """A quiet run has no page-loaded line: final_url falls back, title is None."""
    target = f"{obscura_server}/small"
    rc, content, stderr, final_url, title = await web._run_obscura_safe(
        ["-q", "--allow-private-network", "--dump", "markdown"], target
    )

    assert rc == 0
    assert stderr == ""
    assert final_url == target
    assert title is None
    assert content


@pytest.mark.asyncio
async def test_run_obscura_truncates_stdout(real_obscura, obscura_server):
    """Stdout past 512 KiB is capped before decoding; ASCII output makes it exact."""
    rc, content, _, _, _ = await web._run_obscura_safe(
        ["--allow-private-network", "--dump", "markdown"], f"{obscura_server}/big"
    )

    assert rc == 0
    assert len(content) == web.MAX_STDOUT_BYTES


@pytest.mark.asyncio
async def test_run_obscura_truncates_stderr(real_obscura, obscura_server):
    """Verbose stderr past 64 KiB is capped before decoding."""
    rc, _, stderr_out, _, _ = await web._run_obscura_safe(
        ["-v", "--allow-private-network", "--dump", "markdown"], f"{obscura_server}/big"
    )

    assert rc == 0
    assert len(stderr_out) == web.MAX_STDERR_BYTES


@pytest.mark.asyncio
async def test_run_obscura_propagates_returncode(real_obscura, obscura_server):
    """A blocked fetch exits non-zero and its stderr reaches the caller."""
    target = f"{obscura_server}/small"
    rc, content, stderr, final_url, title = await web._run_obscura_safe(
        ["--dump", "markdown"], target
    )

    assert rc == 1
    assert content == ""
    assert "Error:" in stderr
    assert final_url == target
    assert title is None


@pytest.mark.asyncio
async def test_run_obscura_timeout_kills_real_process_group(
    real_obscura, obscura_server, procfs
):
    """A hung fetch is killed with its process group when timeout_secs elapses."""
    with pytest.raises(TimeoutError, match="timed out"):
        await web._run_obscura_safe(
            ["--allow-private-network", "--dump", "markdown"],
            f"{obscura_server}/hang",
            timeout_secs=0.5,
        )

    await _assert_obscura_gone()


@pytest.mark.asyncio
async def test_run_obscura_cancellation_kills_real_process_group(
    real_obscura, obscura_server, procfs
):
    """External cancellation takes the same killpg path as a timeout."""
    task = asyncio.create_task(
        web._run_obscura_safe(
            ["--allow-private-network", "--dump", "markdown"],
            f"{obscura_server}/hang",
            timeout_secs=30.0,
        )
    )
    try:
        await _wait_obscura_proc(procfs)
        task.cancel()
        with pytest.raises(TimeoutError, match="timed out"):
            await task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await task

    await _assert_obscura_gone()


@pytest.mark.asyncio
async def test_run_obscura_spawn_env_argv_contract(
    real_obscura, obscura_server, procfs, monkeypatch
):
    """The real process sees exact argv with -- and only PATH/HOME in its env."""
    monkeypatch.setenv("OBSCURA_ALLOW_PRIVATE_NETWORK", "1")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    cmd_args = ["--allow-private-network", "--dump", "markdown"]
    target = f"{obscura_server}/hang"

    task = asyncio.create_task(
        web._run_obscura_safe(cmd_args, target, timeout_secs=30.0)
    )
    try:
        _, argv, env_keys = await _wait_obscura_proc(procfs)
        assert argv == [str(real_obscura), "fetch", *cmd_args, "--", target]
        assert env_keys == ["HOME", "PATH"]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await task

    await _assert_obscura_gone()


@pytest.mark.asyncio
async def test_run_obscura_releases_semaphore(real_obscura, obscura_server):
    """All permits return: six concurrent runs, twice the capacity, all complete."""
    results = await asyncio.wait_for(
        asyncio.gather(
            *[
                web._run_obscura_safe(
                    ["--allow-private-network", "--dump", "markdown"],
                    f"{obscura_server}/small",
                )
                for _ in range(6)
            ]
        ),
        timeout=30,
    )

    assert [rc for rc, *_ in results] == [0] * 6
    assert web.OBSCURA_SEMAPHORE._value == 3

