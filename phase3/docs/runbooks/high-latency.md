# Runbook: High Latency

**Alert:** `OrderServiceLatencyCritical` / `OrderServiceLatencyWarning`
**Severity:** Critical / Warning
**SLO Impact:** Tier 1 — Latency SLO (95% of requests under 200ms)
**Last Updated:** 2024-01-01

---

## What Is Happening

Request latency has degraded and the SLO burn rate threshold has been crossed. Users are experiencing slow responses. At critical severity, latency budget will be exhausted in ~2 hours.

---

## Immediate Actions (First 5 Minutes)

**1. Confirm and quantify the latency**
```promql
# Current p50 / p95 / p99 latency
histogram_quantile(0.50, sum by (le) (rate(order_service_request_duration_seconds_bucket[5m])))
histogram_quantile(0.95, sum by (le) (rate(order_service_request_duration_seconds_bucket[5m])))
histogram_quantile(0.99, sum by (le) (rate(order_service_request_duration_seconds_bucket[5m])))
```

**2. Isolate — is it one endpoint or all?**
```promql
histogram_quantile(0.95,
  sum by (le, endpoint) (
    rate(order_service_request_duration_seconds_bucket[5m])
  )
)
```
If it's only `/orders` (POST) — suspect payment service call latency.
If it's all endpoints — suspect Redis or infrastructure.

**3. Check payment service call latency**
```promql
histogram_quantile(0.95,
  sum by (le) (rate(order_payment_call_duration_seconds_bucket[5m]))
)
```
If payment call p95 > 300ms — the latency is coming from payment-service, not order-service.

**4. Check Redis latency**
```promql
histogram_quantile(0.95,
  sum by (le, cmd) (rate(redis_commands_duration_seconds_bucket[5m]))
)
```

**5. Check CPU throttling on pods**
```promql
sum by (pod) (rate(container_cpu_cfs_throttled_seconds_total{namespace="app"}[5m]))
```
High CPU throttling → increase CPU limits or scale out.

---

## Diagnosis Decision Tree

```
High latency detected
│
├── Only POST /orders slow? ─────────────────────────► Payment service slow
│                                                        Follow payment runbook
│                                                        Check FRAUD_AMOUNT_THRESHOLD
│
├── Redis commands slow? ────────────────────────────► Check Redis memory
│   (redis p95 > 10ms)                                  Check eviction rate
│                                                        Consider cache warming
│
├── CPU throttling high? ────────────────────────────► Scale out pods
│   (throttled > 0.5)                                   kubectl scale deployment/order-service
│                                                         --replicas=5 -n app
│
├── Node CPU/memory pressure? ──────────────────────► Cluster autoscaler lag
│                                                        Force scale node group
│
├── Recent deployment? ──────────────────────────────► New code has slow path
│                                                        Check traces for slow spans
│                                                        Rollback if needed
│
└── No obvious cause ────────────────────────────────► Pull traces from Tempo
                                                         Find the slow span
                                                         Escalate to service owner
```

---

## Trace-Based Diagnosis (Tempo)

Open Grafana → Explore → Tempo

```
# Find slow traces
{ .service.name = "order-service" && duration > 500ms }

# Find traces where payment call was slow
{ .service.name = "order-service" } | duration > 200ms
```

Click into any slow trace — the flame graph will show exactly which span is slow.

---

## Quick Mitigations

**Scale out immediately (buys time):**
```bash
kubectl scale deployment/order-service --replicas=6 -n app
kubectl scale deployment/payment-service --replicas=6 -n app
```

**Temporarily increase Redis timeout (if Redis is slow):**
```bash
kubectl set env deployment/order-service REDIS_TIMEOUT=5 -n app
```

**Check HPA — why hasn't it scaled yet?**
```bash
kubectl get hpa -n app
kubectl describe hpa order-service -n app
```

---

## Resolution Checklist

- [ ] Latency back below 200ms p95 for 10+ minutes
- [ ] Root cause identified and documented
- [ ] Any scaling changes made permanent in manifests (not just kubectl patched)
- [ ] Alert resolved
- [ ] Postmortem if latency SLO budget impacted > 5%
