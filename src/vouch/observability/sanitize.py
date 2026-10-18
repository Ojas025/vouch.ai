"""Defensive bounds and redaction for values written into structured logs.

A last line of defence, not a strategy: truncating a secret does not make it
safe to log, so call sites must never pass one in the first place.
"""

from itertools import islice
from typing import Any

MAX_STRING_LEN = 512
MAX_COLLECTION_ITEMS = 20
MAX_DEPTH = 3

TRUNCATION_MARKER = "...[truncated]"
REDACTED = "[REDACTED]"
OMITTED = "...[omitted]"

SENSITIVE_KEY_FRAGMENTS = frozenset(
    {
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "body",
        "completion",
        "cookie",
        "credential",
        "history",
        "passwd",
        "password",
        "prompt",
        "secret",
        "session",
        "signature",
        "system_instruction",
        "token",
    }
)

_TRANSLATION_TABLE = {c: " " for c in [*range(32), 0x7F]}


def is_sensitive_key(key: str) -> bool:
    """True when a field name suggests credentials or raw content."""
    lowered = key.lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def scrub_text(value: str) -> str:
    """Strip control characters and bound length.

    Truncating first matters: ``server`` logs an attacker-sized key off an
    un-sized webhook body, and translating first would allocate a second copy.
    """
    if len(value) > MAX_STRING_LEN:
        return value[:MAX_STRING_LEN].translate(_TRANSLATION_TABLE) + TRUNCATION_MARKER
    return value.translate(_TRANSLATION_TABLE)


def sanitize(value: Any, depth: int = 0) -> Any:
    """JSON-safe, bounded, control-character-free copy of ``value``.

    The depth cap is also what stops a self-referential structure reaching
    ``json.dumps``.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return scrub_text(value)

    if isinstance(value, (list, tuple, set, frozenset)):
        return _sanitize_sequence(value, depth)

    if isinstance(value, dict):
        return _sanitize_mapping(value, depth)

    return scrub_text(str(value))


def _sanitize_sequence(value: Any, depth: int) -> Any:
    if depth >= MAX_DEPTH:
        return OMITTED

    # islice, not list(value)[:N]: materialising the collection defeats the cap.
    bounded = [sanitize(item, depth + 1) for item in islice(value, MAX_COLLECTION_ITEMS)]
    if len(value) > MAX_COLLECTION_ITEMS:
        bounded.append(OMITTED)
    return bounded


def _sanitize_mapping(value: dict, depth: int) -> dict:
    if depth >= MAX_DEPTH:
        return OMITTED

    result: dict[str, Any] = {}
    for key, item in islice(value.items(), MAX_COLLECTION_ITEMS):
        name = scrub_text(str(key))
        if is_sensitive_key(name):
            result[name] = REDACTED
        else:
            result[name] = sanitize(item, depth + 1)

    if len(value) > MAX_COLLECTION_ITEMS:
        result[TRUNCATION_MARKER] = len(value) - MAX_COLLECTION_ITEMS

    return result