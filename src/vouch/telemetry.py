import pyroscope
from pyroscope.otel import PyroscopeSpanProcessor
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from vouch.config import settings


def _span_exporter():
    """Build an OTLP span exporter honouring OTEL_EXPORTER_OTLP_PROTOCOL."""
    if settings.otlp_protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    else:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    return OTLPSpanExporter()


def setup_telemetry():
    pyroscope.configure(
        app_name=settings.otel_service_name,
        server_address=settings.pyroscope_server_address,
    )

    propagate.set_global_textmap(TraceContextTextMapPropagator())

    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(PyroscopeSpanProcessor())
    else:
        new_provider = TracerProvider()
        new_provider.add_span_processor(BatchSpanProcessor(_span_exporter()))
        new_provider.add_span_processor(PyroscopeSpanProcessor())
        trace.set_tracer_provider(new_provider)
