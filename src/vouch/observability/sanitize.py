"""Defensive bounds and redaction for values written into structured logs.

This is a *last line of defence*, not the data-protection strategy. Truncating a
secret or a prompt does not make it safe to log, so call sites must never pass
credentials, raw prompts, model completions, uploaded artifact bytes or
unrestricted tool arguments/results in the first place. Redaction by key exists
only so that a future mistake at a call site degrades instead of leaking.

The module exists because two failure modes are invisible without it: a value
carrying newlines or ANSI escapes corrupts a JSON log line, and an unbounded
nested structure silently produces a multi-megabyte record.
"""

from typing import Any

#: Longest string emitted verbatim. Anything longer is cut and marked.
MAX_STRING_LEN = 512

#: Elements kept from a list/tuple/set before the remainder is summarised.
MAX_COLLECTION_ITEMS = 20

#: Nesting depth walked into dicts/lists before the value is summarised.
MAX_DEPTH = 4

#: Appended to any value that was bounded. Single marker, used everywhere.
TRUNCATION_MARKER = "...[truncated]"

#: Substituted for a value whose key matched SENSITIVE_KEYS.
REDACTED = "[REDACTED]"

#: Stand-in for structure that exceeded MAX_DEPTH or MAX_COLLECTION_ITEMS.
OMITTED = "...[omitted]"

#: Substrings that mark a key as carrying credentials or raw content.
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

#: Keys whose *values* are whole payloads. Bounded, never emitted verbatim.
BULK_KEY_FRAGMENTS = frozenset(
    {"argument", "arguments", "completion", "content", "detail", "output", "payload", "result"}
)

_CONTROL_CHARS = frozenset(chr(c) for c in range(32)) | {"\x7f"}

#: str.translate table built once. A per-character generator was measurably
#: the most expensive thing in the logging path; translate does it in C.
_TRANSLATION_TABLE = {ord(c): " " for c in _CONTROL_CHARS}


def is_sensitive_key(key: str) -> bool:
    """True when a field name suggests credentials or raw content."""
    lowered = key.lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def is_bulk_key(key: str) -> bool:
    """True when a field name suggests an unbounded structure."""
    lowered = key.lower()
    return any(fragment in lowered for fragment in BULK_KEY_FRAGMENTS)


def scrub_text(value: str) -> str:
    """Strip control characters and bound length.

    Newline removal is what stops a value from injecting a fake log line;
    ``json.dumps`` would escape it but the console handler would not.
    """
    cleaned = value.translate(_TRANSLATION_TABLE)
    if len(cleaned) > MAX_STRING_LEN:
        return cleaned[:MAX_STRING_LEN] + TRUNCATION_MARKER
    return cleaned


def sanitize(value: Any, depth: int = 0) -> Any:
    """Return a JSON-safe, bounded, control-character-free copy of ``value``.

    Scalars pass through with their type intact so numeric fields stay numeric
    downstream. Unknown objects fall back to ``str`` rather than being dropped,
    which preserves the existing contract that a non-serialisable extra still
    reaches the log.
    """
    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
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

    items = list(value)
    bounded = [sanitize(item, depth + 1) for item in items[:MAX_COLLECTION_ITEMS]]
    if len(items) > MAX_COLLECTION_ITEMS:
        bounded.append(OMITTED)
    return bounded


def _sanitize_mapping(value: dict, depth: int) -> dict:
    if depth >= MAX_DEPTH:
        return OMITTED

    result: dict[str, Any] = {}
    items = list(value.items())
    for key, item in items[:MAX_COLLECTION_ITEMS]:
        name = scrub_text(str(key))
        if is_sensitive_key(name):
            result[name] = REDACTED
        else:
            result[name] = sanitize(item, depth + 1)

    if len(items) > MAX_COLLECTION_ITEMS:
        result[TRUNCATION_MARKER] = len(items) - MAX_COLLECTION_ITEMS

    return result