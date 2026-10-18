"""Span names and attributes for this service, and a helper that starts one.

A ``vouch.`` prefix: no convention covers an investigation engine. HTTP, DB and
Redis spans are absent because ``opentelemetry-instrument`` emits those.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

INSTRUMENTATION_NAME = "vouch"

# Span names are stable: dashboards, alerts and trace queries key on these.
SPAN_INVESTIGATION_FANOUT = "vouch.investigation.fanout"
SPAN_TOOL_EXECUTE = "vouch.tool.execute"
SPAN_LLM_GENERATE = "vouch.llm.generate"
SPAN_FACT_LOOKUP = "vouch.fact.lookup"
SPAN_JOB_PROCESS = "vouch.job.process"

# Attribute keys. Identifiers and enums only, never payloads.
ATTR_TOOL_NAME = "vouch.tool.name"
ATTR_TOOL_VERSION = "vouch.tool.version"
ATTR_TOOL_CALL_ID = "vouch.tool.call_id"
ATTR_ATTEMPT_NO = "vouch.tool.attempt"
ATTR_DURATION_MS = "vouch.tool.duration_ms"
ATTR_MODALITY = "vouch.modality"
ATTR_ATTEMPT_SOURCE = "vouch.fact.source"
ATTR_FACT_SOURCE_COUNT = "vouch.fact.source_count"
ATTR_FACT_ERROR_COUNT = "vouch.fact.error_count"
ATTR_INVESTIGATION_ID = "vouch.investigation.id"
ATTR_JOB_ID = "vouch.job.id"
ATTR_JOB_IDEMPOTENCY_KEY = "vouch.job.idempotency_key"
ATTR_LLM_MODEL = "vouch.llm.model"


@contextmanager
def span(
    name: str,
    attributes: dict[str, Any] | None = None,
    context: Any = None,
) -> Iterator[trace.Span]:
    """Start a span as the current one; ``context`` adopts a remote parent.

    Never records exceptions itself — the context manager already does on exit.
    """
    tracer = trace.get_tracer(INSTRUMENTATION_NAME)
    with tracer.start_as_current_span(
        name, attributes=attributes or {}, context=context
    ) as active:
        yield active