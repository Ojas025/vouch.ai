"""Trace and correlation fields attached to every LogRecord at creation time.

A record factory is used rather than a filter or formatter because the factory
runs on the thread that emitted the record, inside the caller's context. The
``QueueListener`` that ultimately formats the record runs on its own thread with
no access to the originating task's ``ContextVar``s or span, so anything attached
later would be lost.

The factory chains whatever factory is already installed. Under
``opentelemetry-instrument`` the distro installs its own factory before the
application is imported, and replacing it would silently break its
``LoggingInstrumentor`` injection.
"""

import logging
from typing import Any, Callable

from opentelemetry import trace
from opentelemetry.trace import INVALID_SPAN_CONTEXT

from vouch.observability.context import CONTEXT_VARS, current_correlation

FACTORY_MARKER = "__vouch_record_factory__"

#: Fields this module writes. ``logging.Logger.makeRecord`` raises KeyError if a
#: caller passes one of these through ``extra=``, so they are reserved: emit them
#: by binding the ContextVar instead of shadowing them per call site.
RESERVED_FACTORY_FIELDS = frozenset({"trace_id", "span_id", "trace_sampled", *CONTEXT_VARS})

_previous_factory: Callable[..., logging.LogRecord] | None = None


def _trace_fields() -> dict[str, Any]:
    """Trace identifiers for the active span, or nothing when there is none.

    Absent rather than zero-filled: a fabricated ``"0"`` is indistinguishable
    from a real trace in a dashboard.
    """
    span_context = trace.get_current_span().get_span_context()
    if span_context == INVALID_SPAN_CONTEXT or not span_context.is_valid:
        return {}

    return {
        "trace_id": format(span_context.trace_id, "032x"),
        "span_id": format(span_context.span_id, "016x"),
        "trace_sampled": span_context.trace_flags.sampled,
    }


def vouch_record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    """Chained factory: delegates first, then overlays Vouch correlation."""
    record = _previous_factory(*args, **kwargs)  # type: ignore[misc]

    for key, value in _trace_fields().items():
        if not hasattr(record, key):
            setattr(record, key, value)

    for key, value in current_correlation().items():
        if not hasattr(record, key):
            setattr(record, key, value)

    return record


def install_record_factory() -> bool:
    """Install the chained factory. Returns True when it was newly installed.

    Idempotent by attribute rather than a module flag so a re-import or a
    second call in the same process cannot stack two factories.
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
    """Put back the factory that was installed before ours. True if one was."""
    global _previous_factory

    if _previous_factory is None:
        return False

    logging.setLogRecordFactory(_previous_factory)
    _previous_factory = None
    return True