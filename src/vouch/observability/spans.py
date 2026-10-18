"""Tracer handle and the span names / attributes Vouch defines.

Attribute names use a ``vouch.`` prefix because there is no stable semantic
convention for an investigation engine. The OpenTelemetry GenAI conventions
would fit the LLM spans, but in this version they live only under
``opentelemetry.semconv._incubating.attributes`` -- a private, unstable import
path -- so the names are spelled out here instead and the OTel equivalents are
noted in ``docs/OBSERVABILITY.md``.

No HTTP, database or Redis spans are defined here. ``opentelemetry-instrument``
already emits those in the api container, and adding them by hand would
duplicate every operation.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

INSTRUMENTATION_NAME = "vouch"

# --- Span names. Stable: dashboards and alerts key on these. ---
SPAN_INVESTIGATION = "vouch.investigation"
SPAN_INVESTIGATION_FANOUT = "vouch.investigation.fanout"
SPAN_BRANCH = "vouch.branch.analyze"
SPAN_TOOL_EXECUTE = "vouch.tool.execute"
SPAN_LLM_GENERATE = "vouch.llm.generate"
SPAN_FACT_LOOKUP = "vouch.fact.lookup"
SPAN_JOB_PROCESS = "vouch.job.process"
SPAN_EVIDENCE_PERSIST = "vouch.evidence.persist"

# --- Attributes. Bounded by convention: identifiers and enums only. ---
ATTR_TOOL_NAME = "vouch.tool.name"
ATTR_TOOL_VERSION = "vouch.tool.version"
ATTR_TOOL_SIDE_EFFECTS = "vouch.tool.side_effects"
ATTR_TOOL_CALL_ID = "vouch.tool.call_id"
ATTR_ATTEMPT_NO = "vouch.tool.attempt"
ATTR_DURATION_MS = "vouch.tool.duration_ms"
ATTR_ERROR_CATEGORY = "vouch.error.category"
ATTR_REASON_CODE = "vouch.reason_code"
ATTR_MODALITY = "vouch.modality"
ATTR_ATTEMPT_SOURCE = "vouch.fact.source"
ATTR_FACT_SOURCE_COUNT = "vouch.fact.source_count"
ATTR_FACT_ERROR_COUNT = "vouch.fact.error_count"
ATTR_INVESTIGATION_ID = "vouch.investigation.id"
ATTR_JOB_ID = "vouch.job.id"
ATTR_JOB_IDEMPOTENCY_KEY = "vouch.job.idempotency_key"
ATTR_OUTCOME = "vouch.outcome"

#: Mirrors of the OTel GenAI conventions, for the LLM span. See module note.
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"


def get_tracer() -> trace.Tracer:
    """Tracer for this service. Returns the proxy tracer until a provider is set,
    so spans degrade to no-ops rather than failing when telemetry is absent."""
    return trace.get_tracer(INSTRUMENTATION_NAME)


@contextmanager
def span(
    name: str,
    attributes: dict[str, Any] | None = None,
    context: Any = None,
) -> Iterator[trace.Span]:
    """Start a span as the current one.

    ``context`` adopts a remote parent, which is how a worker continues the
    request's trace instead of starting an unrelated one. Left as ``None`` the
    span parents to whatever is current, or starts a root span.

    Never swallows exceptions, and never records one itself: the OTel context
    manager already calls ``record_exception`` and sets ERROR status on exit.
    Adding a manual record here would double every exception in the span.
    """
    with get_tracer().start_as_current_span(
        name, attributes=attributes or {}, context=context
    ) as active:
        yield active