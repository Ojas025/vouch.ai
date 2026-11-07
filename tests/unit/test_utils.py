"""Spec-driven unit tests for vouch.utils: retry (R1-R9) and delete_from_path (D1-D5)."""

import asyncio

import httpx
import pytest

from vouch.utils import delete_from_path, retry


def _http_status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com/health")
    response = httpx.Response(503, request=request)
    return httpx.HTTPStatusError("service unavailable", request=request, response=response)


NON_TRANSIENT_ERRORS = [
    ValueError("bad input"),
    TypeError("bad type"),
    _http_status_error(),
]

JITTER_ERROR_IDS = ["value_error", "type_error", "http_status_error"]


@pytest.fixture
def sleeps(monkeypatch):
    """Recorded delays passed to asyncio.sleep."""
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


@pytest.fixture
def jitter_draws(monkeypatch):
    """Recorded ranges passed to uniform; draws return the lower bound (worst case)."""
    draws: list[tuple[float, float]] = []

    def fake_uniform(low: float, high: float) -> float:
        draws.append((low, high))
        return low

    monkeypatch.setattr("vouch.utils.uniform", fake_uniform)
    return draws


@pytest.mark.asyncio
async def test_success_on_first_attempt_skips_sleeping(sleeps):
    """R1/R2: result passes through with original args; no retries, no sleeping."""
    calls = {"n": 0}

    @retry(max_attempts=3)
    async def add(a, b=0):
        calls["n"] += 1
        return a + b

    assert await add(2, b=3) == 5
    assert calls["n"] == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_transient_error_is_retried_until_success(sleeps):
    """R2/R3: recovers after transient failures; total calls bounded by max_attempts."""
    calls = {"n": 0}

    @retry(max_attempts=3)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("connection refused")
        return "recovered"

    assert await flaky() == "recovered"
    assert calls["n"] == 3
    assert len(sleeps) == 2


@pytest.mark.asyncio
async def test_raises_the_final_attempt_exception_after_exhaustion(sleeps):
    """R6: once attempts are exhausted the final exception propagates unchanged."""
    raised: list[httpx.ReadTimeout] = []
    calls = {"n": 0}

    @retry(max_attempts=3, base_delay=1, max_delay=10)
    async def always_slow():
        calls["n"] += 1
        error = httpx.ReadTimeout(f"read timed out on attempt {calls['n']}")
        raised.append(error)
        raise error

    with pytest.raises(httpx.ReadTimeout) as exc_info:
        await always_slow()

    assert exc_info.value is raised[-1]
    assert calls["n"] == 3
    assert len(sleeps) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error", NON_TRANSIENT_ERRORS, ids=JITTER_ERROR_IDS)
async def test_non_transient_error_propagates_after_single_call(error, sleeps):
    """R5: only httpx.TransportError is retried; anything else escapes after one call."""
    calls = {"n": 0}

    @retry(max_attempts=3)
    async def broken():
        calls["n"] += 1
        raise error

    with pytest.raises(type(error)):
        await broken()

    assert calls["n"] == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_backoff_sleep_stays_within_equal_jitter_bounds(sleeps, jitter_draws):
    """R4: sleep is drawn from uniform(cap/2, cap), cap = min(max_delay, base_delay * 2**attempt)."""
    max_attempts, base_delay, max_delay = 4, 1, 10
    # caps for attempts 0..2: min(10, 1 * 2**n)
    expected_caps = [1, 2, 4]
    calls = {"n": 0}

    @retry(max_attempts=max_attempts, base_delay=base_delay, max_delay=max_delay)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < max_attempts:
            raise httpx.ConnectError("unreachable")
        return "recovered"

    assert await flaky() == "recovered"
    assert len(sleeps) == len(expected_caps)
    for (low, high), cap, slept in zip(jitter_draws, expected_caps, sleeps, strict=True):
        assert high == cap
        assert low == cap / 2
        assert low <= slept <= cap


@pytest.mark.asyncio
async def test_backoff_cap_never_exceeds_max_delay(sleeps, jitter_draws):
    """R4: exponential growth is clamped at max_delay before the jitter draw."""
    max_attempts, base_delay, max_delay = 4, 8, 10
    # caps for attempts 0..2: min(10, 8 * 2**n)
    expected_caps = [8, 10, 10]
    calls = {"n": 0}

    @retry(max_attempts=max_attempts, base_delay=base_delay, max_delay=max_delay)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < max_attempts:
            raise httpx.ConnectTimeout("too slow")
        return "recovered"

    assert await flaky() == "recovered"
    assert [high for _, high in jitter_draws] == expected_caps


@pytest.mark.asyncio
async def test_max_attempts_one_makes_exactly_one_call(sleeps):
    """R8: max_attempts=1 means a single invocation and no backoff."""
    calls = {"n": 0}

    @retry(max_attempts=1)
    async def doomed():
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        await doomed()

    assert calls["n"] == 1
    assert sleeps == []


@pytest.mark.parametrize("max_attempts", [0, -1], ids=["zero", "negative"])
def test_max_attempts_below_one_raises_at_decoration_time(max_attempts):
    """R9: an invalid attempt count fails fast when the decorator is applied."""
    with pytest.raises(ValueError):

        @retry(max_attempts=max_attempts)
        async def never():
            return None


def test_delete_removes_existing_file(tmp_path):
    """D1: an existing file at path is deleted."""
    target = tmp_path / "media.bin"
    target.write_bytes(b"payload")

    delete_from_path(target)

    assert not target.exists()


def test_delete_missing_path_is_silent_noop(tmp_path):
    """D2: an absent path, flat or nested, raises nothing."""
    delete_from_path(tmp_path / "absent.bin")
    delete_from_path(tmp_path / "no" / "such" / "file.bin")

    assert not (tmp_path / "absent.bin").exists()


def test_delete_refuses_directory_and_preserves_contents(tmp_path):
    """D3: directories raise IsADirectoryError and stay intact."""
    directory = tmp_path / "downloads"
    directory.mkdir()
    (directory / "keep.txt").write_text("keep")

    with pytest.raises(IsADirectoryError):
        delete_from_path(directory)

    assert directory.is_dir()
    assert (directory / "keep.txt").read_text() == "keep"


def test_delete_removes_symlink_but_not_target(tmp_path):
    """D4: only the link entry goes; the target file survives."""
    target = tmp_path / "target.txt"
    target.write_text("payload")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    assert link.is_symlink()

    delete_from_path(link)

    assert not link.is_symlink()
    assert target.read_text() == "payload"


def test_delete_removes_broken_symlink(tmp_path):
    """D5: a dangling symlink is still an entry at path and must be removed."""
    link = tmp_path / "dangling.txt"
    link.symlink_to(tmp_path / "never-written.txt")
    assert link.is_symlink()
    assert not link.exists()

    delete_from_path(link)

    assert not link.is_symlink()
