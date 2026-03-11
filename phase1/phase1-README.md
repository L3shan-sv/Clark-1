# Phase 1 — Application Layer

![Architecture](../docs/images/phase1-architecture.svg)

## Overview

Four production-grade FastAPI microservices, each fully instrumented for metrics, logs, and distributed traces from the first line of code. A shared `observability.py` module ensures every service speaks the same telemetry language — no per-service configuration drift.

**The rule:** telemetry is not an afterthought. It is wired in before the first endpoint is written.

---

## The Four Services

### `order-service`
The user-facing entry point. Accepts order creation and status requests, calls payment-service synchronously, then publishes an event to Kafka.

```
POST /orders          → Create order, call payment, publish event
GET  /orders/{id}     → Order status lookup
GET  /health/live     → Liveness probe
GET  /health/ready    → Readiness probe (checks Redis + payment-service)
GET  /metrics         → Prometheus scrape endpoint
```

### `payment-service`
Processes payments with basic fraud detection. Stateful — maintains transaction records in PostgreSQL.

```
POST /payments        → Process payment (fraud check + charge)
GET  /payments/{id}   → Payment status
GET  /metrics
```

### `notification-service`
Kafka consumer. Reads from `orders` and `payments` topics, routes notifications to email/SMS/Slack based on event type.

```
Kafka consumer: orders, payments topics
Routes → email / SMS / Slack based on event type
GET  /metrics
```

### `analytics-service`
Second Kafka consumer. Aggregates order + payment data into Redis for real-time executive dashboards. Maintains counters, revenue totals, and service health metrics.

```
Kafka consumer: orders, payments topics
Redis aggregates → executive dashboard metrics
GET  /metrics
GET  /health/live
```

---

## Shared Observability Module

`app/shared/observability.py` is the single source of truth for all telemetry. Every service imports it — no duplication.

```python
# What it provides:
configure_logging()    # structlog → JSON → Loki
configure_tracing()    # OTEL TracerProvider → Tempo (OTLP gRPC :4317)
configure_metrics()    # Prometheus /metrics endpoint
ObservabilityMiddleware # Auto-injects trace context into every request/response
```

Every request automatically gets:
- A `trace_id` correlated across all services
- A `span_id` for the specific operation
- Structured JSON log line with `service`, `level`, `duration_ms`, `status_code`
- A Prometheus histogram entry for latency + counter for request rate/errors

---

## Files

```
phase1/
├── app/
│   ├── shared/
│   │   ├── observability.py         # Single source of truth for telemetry
│   │   └── requirements.txt
│   ├── order-service/
│   │   └── main.py                  # FastAPI app + business logic
│   ├── payment-service/
│   │   └── main.py
│   ├── notification-service/
│   │   └── main.py
│   ├── analytics-service/
│   │   └── main.py
│   ├── Dockerfile                   # Shared base image
│   └── docker-compose.yml           # Local development
└── kubernetes/
    └── app/
        └── deployments.yaml         # Production K8s manifests
```

---

## Kubernetes Manifests

`deployments.yaml` includes production-hardened configuration for every service:

```yaml
# Zero-downtime rolling updates
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0     # Never take a pod down before the new one is ready
    maxSurge: 1

# Zone topology spread — pods across 3 AZs
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: DoNotSchedule

# Security hardening
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false

# Graceful shutdown — finish in-flight requests
lifecycle:
  preStop:
    exec:
      command: ["sleep", "5"]
```

---

## Key Metrics Exposed

Every service exposes these Prometheus metrics at `/metrics`:

| Metric | Type | Description |
|---|---|---|
| `{service}_requests_total` | Counter | Request count by `method`, `path`, `status_code` |
| `{service}_request_duration_seconds` | Histogram | Latency — used for p95/p99 SLOs |
| `{service}_active_requests` | Gauge | Current in-flight requests |
| `kafka_consumer_lag` | Gauge | Messages behind for consumer services |
| `redis_operation_duration_seconds` | Histogram | Cache operation latency |

---

## Local Development

```bash
cd phase1/app
docker-compose up

# Services available at:
# order-service:       http://localhost:8000
# payment-service:     http://localhost:8001
# notification-service: http://localhost:8002
# analytics-service:   http://localhost:8003
# Grafana:             http://localhost:3000
# Prometheus:          http://localhost:9090
```

---

## Deploy to Kubernetes

```bash
# Build and push images
docker build -t your-registry/order-service:latest ./app/order-service
docker push your-registry/order-service:latest
# ... repeat for each service

# Deploy
kubectl apply -f kubernetes/app/deployments.yaml

# Verify
kubectl get pods -n app
kubectl logs -n app deployment/order-service --tail=20
```

---

## Design Decisions

### Why a shared observability module?
Four separate telemetry setups means four places to update when Tempo's endpoint changes, or when you add a new structured log field. The shared module is the single source of truth — update once, all services get the change on next deploy.

### Why structlog over the standard `logging` module?
structlog outputs machine-readable JSON natively. Standard library logging requires a JSON formatter and loses context between calls. structlog's `bind()` lets you accumulate context (user_id, order_id, trace_id) across a request lifetime without passing it through every function call.

### Why OTLP gRPC for traces over HTTP?
gRPC is binary (smaller payloads) and uses HTTP/2 (multiplexed connections). At high request rates, the difference matters. Tempo's OTLP gRPC port `:4317` is the standard.

---

## What's Next

[Phase 2 →](../phase2/README.md) — Define SLOs, error budgets, and multi-window burn rate alerts on top of these metrics
