"""W3C trace-context capture and replay across the Redis stream boundary.

Captured at write time and replayed at publish: the relay is another process,
so injecting its own span would trace the relay instead.
"""

from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

TRACEPARENT_KEY = "traceparent"
TRACESTATE_KEY = "tracestate"

#: The only keys allowed to cross the broker boundary.
CARRIER_KEYS = frozenset({TRACEPARENT_KEY, TRACESTATE_KEY})

#: W3C bounds traceparent at 55 bytes and tracestate at 512.
MAX_CARRIER_LEN = 512

_PROPAGATOR = TraceContextTextMapPropagator()


def capture_trace_context() -> dict[str, str]:
    """W3C carrier for the active span, or ``{}`` — never a fabricated one."""
    if not trace.get_current_span().get_span_context().is_valid:
        return {}

    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier


def inject_trace_context(body: dict[str, Any], carrier: dict[str, str]) -> dict[str, Any]:
    """Merge the carrier into ``body`` in place and return it.

    Baggage is excluded: it is arbitrary application state, not trace context.
    """
    for key in CARRIER_KEYS:
        if key in carrier:
            body[key] = carrier[key]
    return body


def extract_trace_context(body: dict[str, Any]) -> Context:
    """Rebuild the originating context, or empty for legacy or malformed events.

    ``body`` comes from a Redis message, so both keys are type- and length-checked:
    a non-string raises inside OTel's parser and an unbounded value feeds its regex.
    """
    carrier = {
        key: body[key]
        for key in CARRIER_KEYS
        if isinstance(body.get(key), str) and len(body[key]) <= MAX_CARRIER_LEN
    }
    if TRACEPARENT_KEY not in carrier:
        return otel_context.Context()
    return _PROPAGATOR.extract(carrier=carrier)