"""OpenTelemetry span emitter → Langfuse OTLP HTTP.

Canonical endpoint: ${LANGFUSE_HOST}/api/public/otel/v1/traces (env LANGFUSE_OTLP_ENDPOINT).
Span name: llm-proxy-request. Attributes match PRD-CONN-0045 §"Observability":
input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens,
route_taken, fallback_reason.
"""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger("proxy.otel")

_initialised = False


def init_tracing(service_name: str = "arcanada-llm-proxy") -> None:
    global _initialised
    if _initialised:
        return
    endpoint = os.environ.get("LANGFUSE_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        log.info("LANGFUSE_OTLP_ENDPOINT unset — tracing disabled (no-op)")
        _initialised = True
        return
    headers_env = os.environ.get("LANGFUSE_OTLP_HEADERS", "")
    headers = _parse_headers(headers_env)
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers))
    )
    trace.set_tracer_provider(provider)
    _initialised = True
    log.info("tracing initialised endpoint=%s", endpoint)


def _parse_headers(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k.strip()] = v.strip()
    return out


@contextmanager
def request_span(attributes: dict[str, str | int | float]) -> Iterator[trace.Span]:
    tracer = trace.get_tracer("arcanada-llm-proxy")
    with tracer.start_as_current_span("llm-proxy-request") as span:
        for k, v in attributes.items():
            try:
                span.set_attribute(k, v)
            except Exception:
                pass
        yield span
