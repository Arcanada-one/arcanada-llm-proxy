"""OpenTelemetry span emitter → Langfuse OTLP HTTP.

Canonical endpoint: ${LANGFUSE_HOST}/api/public/otel/v1/traces (env LANGFUSE_OTLP_ENDPOINT).
Span name: llm-proxy-request. Attributes match PRD-CONN-0045 §"Observability":
input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens,
route_taken, fallback_reason.

Also hosts SecretRedactor — defensive logging filter that strips bearer tokens
and an explicit shared_secret substring before any handler emits the record.
"""

import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger("proxy.otel")

_initialised = False

_BEARER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1***REDACTED***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.=]{8,}"), r"\1***REDACTED***"),
)
_MIN_SECRET_LEN = 8


class SecretRedactor(logging.Filter):
    """Strip bearer tokens + caller-supplied secret substrings from log records."""

    def __init__(self, extra_secrets: list[str] | None = None) -> None:
        super().__init__()
        self._extras = [s for s in (extra_secrets or []) if s and len(s) >= _MIN_SECRET_LEN]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for pat, repl in _BEARER_PATTERNS:
            redacted = pat.sub(repl, redacted)
        for extra in self._extras:
            redacted = redacted.replace(extra, "***REDACTED***")
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def install_log_redaction(extra_secrets: list[str] | None = None) -> SecretRedactor:
    """Attach SecretRedactor to root logger + every existing handler.

    Filters on a Logger fire only for records originating from that logger;
    handler-level filters fire for every record an ancestor logger forwards
    to that handler — so we install on both for defence-in-depth.
    """
    f = SecretRedactor(extra_secrets=extra_secrets)
    root = logging.getLogger()
    root.addFilter(f)
    for h in root.handlers:
        h.addFilter(f)
    return f


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
