"""Unit tests for the web tools suite.

Derived from docs/security/web.md (§1-§12) and the web tools test plan.
Offline by construction: httpx uses MockTransport, getaddrinfo is stubbed on
the event loop, and Obscura subprocess calls are replaced with fakes.
"""

import asyncio
import contextlib
import hashlib
import ipaddress
import signal
import socket
import struct
import tempfile
import time
import unicodedata
from asyncio.base_events import BaseEventLoop
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

import vouch.tools.tools.web as web
from vouch.security import TrustLevel
from vouch.tools.tools.web import (
    MAX_REDIRECTS,
    ExtractPageContentArgs,
    ScreenshotPageArgs,
    UrlPolicyVerdict,
    WebInspectArgs,
    extract_links_from_markdown,
    extract_page_content,
    screenshot_page,
    validate_url,
    web_inspect,
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
        ("https://", UrlPolicyVerdict.INVALID_SYNTAX),
        ("ftp://example.com/file", UrlPolicyVerdict.UNSUPPORTED_SCHEME),
        ("file:///etc/passwd", UrlPolicyVerdict.UNSUPPORTED_SCHEME),
        ("javascript:alert(1)", UrlPolicyVerdict.UNSUPPORTED_SCHEME),
        ("data:text/html,<h1>x</h1>", UrlPolicyVerdict.UNSUPPORTED_SCHEME),
    ],
    ids=[
        "empty", "oversized", "space", "tab", "cr", "nl", "backslash",
        "userinfo", "no-host", "ftp", "file", "javascript", "data",
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
        "loopback", "rfc1918-10", "rfc1918-192", "cloud-metadata",
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
    assert out.trust is TrustLevel.DETERMINISTIC
    assert out.provenance is not None
    assert out.provenance.producer_type == "SERVICE"
    assert out.provenance.producer_id == "vouch_web_inspect"
    assert out.provenance.tool_name == "web_inspect"
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
    else:
        def timeout_handler(request):
            raise httpx.ConnectTimeout("connect timeout")

        _mock_httpx_client(monkeypatch, timeout_handler)
        out = await web_inspect(WebInspectArgs(url="http://slow.test/"))
        assert out.safety_status is UrlPolicyVerdict.ERROR
        assert out.error == "ConnectTimeout"


# ============================== extract_links_from_markdown ==============================


@pytest.mark.parametrize(
    "content,base,expected_urls",
    [
        (
            "[Download](/download.apk)\n[Verify](../verify)",
            "https://site.test/posts/2026/item",
            ["https://site.test/download.apk", "https://site.test/posts/verify"],
        ),
        ("[Login](https://site.com/login#form)", "https://site.com/", ["https://site.com/login"]),
        (
            "[Click](javascript:void(0))\n[Top](#top)\njavascript:void(0)\n"
            "[Real](https://site.com/ok)",
            "https://site.com/",
            ["https://site.com/ok"],
        ),
        ("see https://site.test/x now", "https://site.test/", ["https://site.test/x"]),
        (
            "\n".join(f"[Link {i}](https://site.test/p{i})" for i in range(500)),
            "https://site.test/",
            [f"https://site.test/p{i}" for i in range(50)],
        ),
    ],
    ids=["relative", "fragment-stripped", "javascript-and-anchors-dropped", "bare-raw-url", "cap-at-50"],
)
def test_extract_links_normalizes_filters_and_caps(content, base, expected_urls):
    """Link derivation normalizes targets, drops unsafe schemes, and caps output."""
    links = extract_links_from_markdown(content, base)

    assert [link.url for link in links] == expected_urls
    for link in links:
        assert link.derived_from == base
        assert link.trust is TrustLevel.UNTRUSTED_ARTIFACT
        assert link.provenance is not None


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
    ],
    ids=["full-page", "spa-shell", "bidi-stripped"],
)
@pytest.mark.asyncio
async def test_extract_success_output(monkeypatch, case):
    """§8/§10/§12: parsed output is sanitized, re-validated, and fully tagged."""
    stdout, stderr, expected_final, expected_title, expected_links, expected_empty, visible = case
    _fake_obscura(monkeypatch, stdout=stdout, stderr=stderr)

    out = await extract_page_content(ExtractPageContentArgs(url="http://start.test/page"))

    assert out.error is None
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
    "scenario", ["rc-failure", "timeout"], ids=["obscura-rc-failure", "timeout"]
)
@pytest.mark.asyncio
async def test_extract_failure_modes(monkeypatch, scenario):
    """Obscura failures and timeouts map to stable error outputs."""
    if scenario == "rc-failure":
        _fake_obscura(monkeypatch, returncode=1)
        out = await extract_page_content(ExtractPageContentArgs(url="http://start.test/page"))
        assert out.error == "obscura_failed_1"
    else:
        monkeypatch.setattr(web, "_run_obscura_safe", AsyncMock(side_effect=TimeoutError))
        out = await extract_page_content(ExtractPageContentArgs(url="http://start.test/page"))
        assert out.error == "timeout"
        assert out.markdown_content is None


# ============================== pre-flight gate (both tools) ==============================


@pytest.mark.parametrize(
    "tool,args",
    [
        (extract_page_content, ExtractPageContentArgs(url="http://127.0.0.1/")),
        (extract_page_content, ExtractPageContentArgs(url="http://169.254.169.254/latest/meta-data/")),
        (screenshot_page, ScreenshotPageArgs(url="http://127.0.0.1/")),
        (screenshot_page, ScreenshotPageArgs(url="http://169.254.169.254/latest/meta-data/")),
    ],
    ids=["extract-loopback", "extract-metadata", "screenshot-loopback", "screenshot-metadata"],
)
@pytest.mark.asyncio
async def test_blocks_private_url_before_spawn(monkeypatch, tool, args):
    """§3: a private initial URL never reaches the Obscura subprocess."""
    obscura = AsyncMock()
    monkeypatch.setattr(web, "_run_obscura_safe", obscura)

    out = await tool(args)

    assert out.error == "blocked_private_ip"
    obscura.assert_not_awaited()


# ============================== screenshot_page ==============================


@pytest.mark.parametrize(
    "width,height", [(1280, 720), (320, 240)], ids=["hd", "small"]
)
@pytest.mark.asyncio
async def test_screenshot_success_content_addressed(monkeypatch, tmp_path, width, height):
    """§11/§12: a valid capture lands at <sha256>.png behind an opaque handle."""
    _patch_artifacts_dir(monkeypatch, tmp_path)
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
    assert out.provenance.observed_at is not None
    assert out.provenance.artifact_sha256 == digest


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
    monkeypatch, tmp_path, png_bytes, returncode, expected_error
):
    """§11: corrupt or failed captures never reach storage and leave no scratch dirs."""
    artifacts_dir = _patch_artifacts_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        web, "_run_obscura_safe", _obscura_writes_capture(png_bytes, returncode=returncode)
    )
    tempdirs = []
    real_tempdir = tempfile.TemporaryDirectory

    def recording_tempdir(*args, **kwargs):
        tmpdir = real_tempdir(*args, **kwargs)
        tempdirs.append(tmpdir.name)
        return tmpdir

    monkeypatch.setattr(tempfile, "TemporaryDirectory", recording_tempdir)

    out = await screenshot_page(ScreenshotPageArgs(url="https://site.test/page"))

    assert out.error == expected_error
    assert out.artifact_id is None
    assert list(artifacts_dir.iterdir()) == []
    assert tempdirs
    for path in tempdirs:
        assert not Path(path).exists()


@pytest.mark.parametrize(
    "final_url",
    ["http://127.0.0.1/steal", "http://169.254.169.254/steal"],
    ids=["loopback", "cloud-metadata"],
)
@pytest.mark.asyncio
async def test_screenshot_blocks_post_redirect_final_url(monkeypatch, tmp_path, final_url):
    """§10: a capture that landed on a private URL is rejected before staging."""
    artifacts_dir = _patch_artifacts_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        web,
        "_run_obscura_safe",
        _obscura_writes_capture(_png_bytes(800, 600), final_url=final_url),
    )

    out = await screenshot_page(ScreenshotPageArgs(url="https://site.test/page"))

    assert out.error == "redirect_blocked_blocked_private_ip"
    assert out.artifact_id is None
    assert list(artifacts_dir.iterdir()) == []


# ============================== _run_obscura_safe ==============================


@pytest.mark.parametrize(
    "cmd_args",
    [["--dump", "markdown"], ["-s", "/tmp/capture.png"]],
    ids=["dump-markdown", "screenshot"],
)
@pytest.mark.asyncio
async def test_run_obscura_spawn_isolation_contract(monkeypatch, cmd_args):
    """§4/§5/§6: isolated process group, sanitized env, option-injection-proof argv."""
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("OBSCURA_ALLOW_PRIVATE_NETWORK", "1")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    target = "--allow-private-network"

    await web._run_obscura_safe(cmd_args, target)

    kwargs = captured["kwargs"]
    assert kwargs.get("env") is not None
    assert kwargs["env"] == {
        "PATH": "/home/ojas/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
    }
    assert kwargs["start_new_session"] is True
    args = list(captured["args"])
    assert args[:2] == ["obscura", "fetch"]
    assert args[2 : 2 + len(cmd_args)] == cmd_args
    assert args[-2:] == ["--", target]


@pytest.mark.parametrize(
    "case",
    ["stdout-cap", "stderr-cap", "invalid-utf8", "page-loaded", "no-page-loaded"],
)
@pytest.mark.asyncio
async def test_run_obscura_bounded_sanitized_output(monkeypatch, case):
    """§9: output buffers are capped and malformed bytes decode without crashing."""
    if case == "stdout-cap":
        stdout, stderr = b"A" * (512 * 1024 + 999), b""
    elif case == "stderr-cap":
        stdout, stderr = b"", b"B" * (64 * 1024 + 123)
    elif case == "invalid-utf8":
        stdout, stderr = b"ok\xff\xfe fine", b""
    elif case == "page-loaded":
        stdout, stderr = b"body", b'Page loaded: https://x.test/y - "T"'
    else:
        stdout, stderr = b"body", b""
    _fake_obscura(monkeypatch, stdout=stdout, stderr=stderr)

    rc, content, stderr_out, final_url, title = await web._run_obscura_safe(
        ["--dump", "markdown"], "https://site.test/start"
    )

    assert rc == 0
    if case == "stdout-cap":
        assert len(content) == 512 * 1024
    elif case == "stderr-cap":
        assert len(stderr_out) == 64 * 1024
    elif case == "invalid-utf8":
        assert "\ufffd" in content
    elif case == "page-loaded":
        assert final_url == "https://x.test/y"
        assert title == "T"
    else:
        assert final_url == "https://site.test/start"
        assert title is None


@pytest.mark.parametrize(
    "killpg_mode", ["records", "raises-lookup"], ids=["records", "process-lookup-error"]
)
@pytest.mark.asyncio
async def test_run_obscura_timeout_kills_process_group(monkeypatch, killpg_mode):
    """§4: a timed-out Obscura is SIGKILLed via its process group and awaited."""
    spawned = []
    killed = []

    async def fake_exec(*args, **kwargs):
        proc = FakeProcess(hang=True)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    if killpg_mode == "records":
        monkeypatch.setattr(web.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    else:
        def raise_lookup(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(web.os, "killpg", raise_lookup)

    with pytest.raises(TimeoutError, match="timed out"):
        await web._run_obscura_safe(
            ["--dump", "markdown"], "http://site.test/", timeout_secs=0.05
        )

    if killpg_mode == "records":
        assert killed == [(spawned[0].pid, signal.SIGKILL)]
    assert spawned[0].wait_called is True


@pytest.mark.asyncio
async def test_run_obscura_cancellation_kills_process_group(monkeypatch):
    """§4: external cancellation takes the same killpg path as a timeout."""
    spawned = []
    killed = []

    async def fake_exec(*args, **kwargs):
        proc = FakeProcess(hang=True)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(web.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    task = asyncio.create_task(
        web._run_obscura_safe(["--dump", "markdown"], "http://site.test/", timeout_secs=10.0)
    )
    try:
        for _ in range(100):
            if spawned:
                break
            await asyncio.sleep(0)
        assert spawned, "obscura subprocess was never spawned"
        await asyncio.wait_for(spawned[0].communicate_started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(TimeoutError, match="timed out"):
            await task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await task

    assert killed == [(spawned[0].pid, signal.SIGKILL)]
    assert spawned[0].wait_called is True
