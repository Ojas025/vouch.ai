"""Attaches trace and correlation fields to every LogRecord at creation time.

A record factory rather than a filter or formatter because it runs on the
thread that emitted the record, inside the caller's context.
"""

import logging
from typing import Any, Callable

from opentelemetry import trace

from vouch.observability.context import CONTEXT_VARS, current_correlation

FACTORY_MARKER = "__vouch_record_factory__"

_previous_factory: Callable[..., logging.LogRecord] | None = None


def vouch_record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    """Delegates to the previous factory, then overlays Vouch correlation."""
    record = _previous_factory(*args, **kwargs)  # type: ignore[misc]

    span_context = trace.get_current_span().get_span_context()
    # Absent rather than zero-filled: a fake "0" is indistinguishable from a real trace.
    fields: dict[str, Any] = {} if not span_context.is_valid else {
        "trace_id": format(span_context.trace_id, "032x"),
        "span_id": format(span_context.span_id, "016x"),
        "trace_sampled": span_context.trace_flags.sampled,
    }
    fields.update(current_correlation())

    for key, value in fields.items():
        if not hasattr(record, key):
            setattr(record, key, value)

    return record


def install_record_factory() -> bool:
    """Install the chained factory; True when newly installed.

    Idempotent by attribute, not a module flag, which a re-import resets while
    logging's global factory registry does not.
    """
    global _previous_factory

    existing = logging.getLogRecordFactory()
    if getattr(existing, FACTORY_MARKER, False):
        return False

    _previous_factory = existing
    factory = vouch_record_factory
    setattr(factory, FACTORY_MARKER, True)
    logging.setLogRecordFactory(factory)
    return True


def restore_record_factory() -> bool:
    """Put back the factory installed before ours; True if there was one."""
    global _previous_factory

    if _previous_factory is None:
        return False

    logging.setLogRecordFactory(_previous_factory)
    _previous_factory = None
    return True