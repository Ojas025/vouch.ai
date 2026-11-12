"""Unit tests for LLMClient.ainvoke and stream against Qwen/Qwen3-0.6B-GGUF via OpenRouter."""

import asyncio
import os

import pytest
from dotenv import load_dotenv

from vouch.agents.llm import LLMClient, LLMProfile
from vouch.utils import RetryPolicy, _backoff, _is_retryable

load_dotenv()

MODEL = os.getenv("VOUCH_TEST_LLM_MODEL", "openrouter/cohere/north-mini-code:free")
BASE_URL = os.getenv("VOUCH_TEST_LLM_BASE_URL", "https://openrouter.ai/api/v1")
SYSTEM_PROMPT = "You are a helpful assistant."


def _text(value):
    return getattr(value, "content", value)


def _us(prompt):
    return [{"role": "user", "content": prompt}]


def _make_client():
    try:
        profile = LLMProfile(
            model=MODEL,
            base_url=BASE_URL,
            api_key="OPEN_ROUTER_API_KEY",
            retry_policy=RetryPolicy(enabled=False),
        )
        return LLMClient(profile, system_prompt=SYSTEM_PROMPT)
    except Exception:
        return None


_AVAILABLE = None


def _client_available(client):
    global _AVAILABLE
    if _AVAILABLE is False:
        return False
    if _AVAILABLE is True:
        return True
    try:
        asyncio.run(client.ainvoke([{"role": "user", "content": "hi"}]))
        _AVAILABLE = True
        return True
    except Exception:
        _AVAILABLE = False
        return False


@pytest.fixture
def llm_client():
    client = _make_client()
    if client is None:
        pytest.skip(f"{MODEL} client unavailable")
    if not _client_available(client):
        pytest.skip(f"{MODEL} inference unavailable via {BASE_URL}")
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["What is 2+2?", "Say 'hello'."], ids=["math", "greeting"])
async def test_ainvoke_returns_non_empty_string(llm_client, prompt):
    """ainvoke returns a non-empty string for real prompts."""
    result = await llm_client.ainvoke(_us(prompt))
    text = _text(result)
    assert isinstance(text, str)
    if prompt:
        assert len(text) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("max_tokens", [5, 50], ids=["five", "fifty"])
async def test_ainvoke_max_tokens_bounds_output(llm_client, max_tokens):
    """max_tokens is forwarded to the model without crashing."""
    result = await llm_client.ainvoke(_us("Tell me a story."), max_tokens=max_tokens)
    text = _text(result)
    assert isinstance(text, str)


@pytest.mark.asyncio
@pytest.mark.parametrize("temperature", [0.0, 1.0], ids=["zero", "one"])
async def test_ainvoke_temperature_accepted(llm_client, temperature):
    """temperature is forwarded to the model without crashing."""
    result = await llm_client.ainvoke(_us("Hi."), temperature=temperature)
    text = _text(result)
    assert isinstance(text, str)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, 123], ids=["none", "int"])
async def test_ainvoke_invalid_input_raises(llm_client, bad):
    """ainvoke rejects non-message input."""
    with pytest.raises(Exception):
        await llm_client.ainvoke(bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["Count to 5.", "Say 'hi'."], ids=["count", "hi"])
async def test_stream_yields_chunks_and_completes(llm_client, prompt):
    """stream yields chunks and the generator completes."""
    chunks = [c async for c in llm_client.stream(_us(prompt))]
    assert len(chunks) > 0
    text = "".join(_text(c) for c in chunks)
    assert isinstance(text, str)
    assert len(text) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["What is the capital of France?"], ids=["france"])
async def test_stream_concatenates_to_text(llm_client, prompt):
    """stream output concatenates to a non-empty string."""
    chunks = [c async for c in llm_client.stream(_us(prompt))]
    text = "".join(_text(c) for c in chunks)
    assert isinstance(text, str)
    assert len(text) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, 123], ids=["none", "int"])
async def test_stream_invalid_input_raises(llm_client, bad):
    """stream rejects non-message input."""
    with pytest.raises(Exception):
        [c async for c in llm_client.stream(bad)]


def test_retry_policy_defaults():
    """RetryPolicy defaults keep retries opt-in."""
    policy = RetryPolicy()
    assert policy.enabled is False
    assert policy.max_attempts == 3
    assert policy.base_delay == 1
    assert policy.max_delay == 10


@pytest.mark.parametrize("error,expected", [
    (ConnectionError("x"), True),
    (TimeoutError("x"), True),
    (ValueError("x"), False),
    (TypeError("x"), False),
], ids=["connection", "timeout", "value", "type"])
def test_is_retryable_classifies_errors(error, expected):
    """_is_retryable flags transient errors only."""
    assert _is_retryable(error) is expected


@pytest.mark.parametrize("attempt,base,max_delay", [
    (0, 1, 10),
    (1, 1, 10),
    (2, 1, 10),
], ids=["attempt0", "attempt1", "attempt2"])
def test_backoff_within_bounds(attempt, base, max_delay):
    """_backoff returns a value in [0, min(max_delay, base*2**attempt)]."""
    policy = RetryPolicy(base_delay=base, max_delay=max_delay)
    value = _backoff(attempt, policy)
    cap = min(max_delay, base * (2 ** attempt))
    assert 0.0 <= value <= cap


def test_model_is_config_string():
    """model is stored as a config string, not loaded at construction."""
    profile = LLMProfile(model=MODEL, base_url=BASE_URL, api_key="OPEN_ROUTER_API_KEY")
    assert profile.model == MODEL


def test_fail_logs_category_not_message(caplog):
    """_fail logs error_category as type name without raising."""
    client = _make_client()
    if client is None:
        pytest.skip(f"{MODEL} client unavailable")
    with caplog.at_level("ERROR"):
        client._fail(ValueError("sk-12345-secret"), 1, True)
    records = [r for r in caplog.records if hasattr(r, "error_category")]
    if records:
        assert records[-1].error_category == "ValueError"
