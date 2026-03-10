# shared/observability.py
# Every service imports from here — single source of truth for instrumentation

import logging
import sys
import time
from functools import wraps
from typing import Optional

import structlog
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from fastapi import Request, Response
import structlog


def setup_logging(service_name: str) -> structlog.BoundLogger:
    """
    Structured JSON logging — every log is queryable in Loki.
    Fields: timestamp, level, service, trace_id, span_id, message + any extras
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
    )

    return structlog.get_logger().bind(service=service_name)


def setup_tracing(service_name: str, otlp_endpoint: str = "http://otel-collector:4317"):
    """
    OpenTelemetry tracing — sends spans to OTEL collector → Tempo.
    Every inbound request and outbound call gets a span automatically.
    """
    resource = Resource.create({
        "service.name": service_name,
        "service.version": "1.0.0",
        "deployment.environment": "production",
    })

    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Auto-instrument outbound HTTP and Redis calls
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()

    return trace.get_tracer(service_name)


def setup_metrics(service_name: str, otlp_endpoint: str = "http://otel-collector:4317"):
    """
    OTEL metrics — also exported to Prometheus via /metrics endpoint.
    """
    resource = Resource.create({"service.name": service_name})
    exporter = OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=10000)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)
    return metrics.get_meter(service_name)


def instrument_app(app, service_name: str):
    """Auto-instrument FastAPI — adds spans for every route automatically."""
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=trace.get_tracer_provider(),
        excluded_urls="health,metrics",
    )


# ── Prometheus Metrics Helpers ─────────────────────────────────────────────────
# Use these in each service for business-aware metrics

def make_request_counter(service: str, extra_labels: list = None):
    labels = ["method", "endpoint", "status_code"] + (extra_labels or [])
    return Counter(
        f"{service}_requests_total",
        f"Total HTTP requests for {service}",
        labels,
    )

def make_request_latency(service: str, extra_labels: list = None):
    labels = ["method", "endpoint"] + (extra_labels or [])
    return Histogram(
        f"{service}_request_duration_seconds",
        f"HTTP request latency for {service}",
        labels,
        buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0, 2.5, 5.0],
    )

def make_active_requests(service: str):
    return Gauge(
        f"{service}_active_requests",
        f"Number of in-flight requests in {service}",
    )


# ── Middleware ─────────────────────────────────────────────────────────────────

class ObservabilityMiddleware:
    """
    Injects trace_id and request_id into every log line.
    Tracks request latency and status automatically.
    """

    def __init__(self, app, service_name: str, request_counter, latency_histogram, active_gauge):
        self.app = app
        self.service = service_name
        self.request_counter = request_counter
        self.latency_histogram = latency_histogram
        self.active_gauge = active_gauge

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        start = time.perf_counter()
        status_code = 500

        # Bind trace context to all logs in this request
        current_span = trace.get_current_span()
        ctx = current_span.get_span_context()
        structlog.contextvars.bind_contextvars(
            trace_id=format(ctx.trace_id, "032x") if ctx.is_valid else "none",
            span_id=format(ctx.span_id, "016x") if ctx.is_valid else "none",
            method=request.method,
            path=request.url.path,
        )

        self.active_gauge.inc()
        try:
            async def send_wrapper(message):
                nonlocal status_code
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                await send(message)

            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            self.active_gauge.dec()

            endpoint = request.url.path
            self.request_counter.labels(
                method=request.method,
                endpoint=endpoint,
                status_code=str(status_code),
            ).inc()
            self.latency_histogram.labels(
                method=request.method,
                endpoint=endpoint,
            ).observe(duration)

            structlog.contextvars.unbind_contextvars("trace_id", "span_id", "method", "path")


async def metrics_endpoint(request: Request) -> Response:
    """Expose Prometheus metrics at /metrics."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
