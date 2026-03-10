# Runbook: High Error Rate

**Alert:** `OrderServiceAvailabilityCritical` / `PaymentServiceAvailabilityCritical`
**Severity:** Critical
**SLO Impact:** Tier 1 — User Facing
**Last Updated:** 2024-01-01

---

## What Is Happening

The error rate for this service has exceeded the SLO threshold. The multi-window burn rate alert has fired meaning this is a **sustained** degradation — not a transient blip.

At the current burn rate, the monthly error budget will be exhausted within 2 hours.

---

## Immediate Actions (First 5 Minutes)

**1. Confirm the alert is real**
```promql
# Check current error rate
sum(rate(order_service_requests_total{status_code=~"5.."}[5m]))
/
sum(rate(order_service_requests_total[5m]))
```
If this is below 0.1%, the alert may be resolving. Monitor for 2 minutes before standing down.

**2. Check if a deployment happened recently**
```bash
kubectl rollout history deployment/order-service -n app
kubectl rollout history deployment/payment-service -n app
```
If a deployment happened in the last 30 minutes — **rollback immediately:**
```bash
kubectl rollout undo deployment/order-service -n app
# Verify rollback
kubectl rollout status deployment/order-service -n app
```

**3. Check pod health**
```bash
kubectl get pods -n app -l app=order-service
kubectl describe pod <crashing-pod-name> -n app
kubectl logs <pod-name> -n app --previous --tail=100
```

**4. Check upstream dependencies**
```bash
# Redis health
kubectl exec -n app deployment/order-service -- redis-cli -u $REDIS_URL ping

# Payment service health (from order service)
kubectl exec -n app deployment/order-service -- curl -s http://payment-service:8000/health/ready
```

---

## Diagnosis Decision Tree

```
High error rate detected
│
├── Recent deployment? ──────────────────────────────► ROLLBACK immediately
│                                                        (see step 2 above)
│
├── Pods CrashLooping? ──────────────────────────────► Check logs + OOMKill
│   kubectl get pods -n app                             Increase memory limits
│                                                        if OOMKilled
│
├── Redis unreachable? ──────────────────────────────► Check Redis pod health
│   (errors contain "redis" or "connection refused")    kubectl get pods -n app -l app=redis
│
├── Payment service returning 5xx? ─────────────────► Follow payment-service runbook
│   (order service errors contain "payment")            This is a dependency failure
│
├── Elevated latency + errors? ─────────────────────► Database/Redis slowness
│   (p99 > 1s AND error rate high)                      Check slow queries
│
└── No obvious cause ────────────────────────────────► Check recent config changes
                                                         Escalate to service owner
```

---

## Grafana Queries for Diagnosis

```promql
# Error rate by endpoint
sum by (endpoint) (rate(order_service_requests_total{status_code=~"5.."}[5m]))

# Error rate by status code — distinguish 500 vs 502 vs 503
sum by (status_code) (rate(order_service_requests_total[5m]))

# Which pods are erroring
sum by (pod) (rate(order_service_requests_total{status_code=~"5.."}[5m]))

# Payment service call failures from order service
rate(order_payment_call_duration_seconds_count{status="error"}[5m])
```

---

## Loki Log Queries

```logql
# All errors in order service
{namespace="app", app="order-service"} | json | level="error"

# Errors with trace IDs (click trace_id to jump to Tempo)
{namespace="app", app="order-service"} | json | level="error" | line_format "{{.trace_id}} {{.message}}"

# Payment timeout errors specifically
{namespace="app", app="order-service"} | json | message =~ "payment.*timeout"
```

---

## Escalation

| Time Without Resolution | Action |
|---|---|
| 15 minutes | Page engineering lead |
| 30 minutes | Page service owner + VP Engineering |
| 1 hour | Incident bridge — all hands |

---

## Resolution Checklist

- [ ] Root cause identified
- [ ] Fix deployed or rollback executed
- [ ] Error rate back below SLO threshold for 10+ minutes
- [ ] Alert resolved in Alertmanager
- [ ] Incident closed in Grafana OnCall
- [ ] Postmortem scheduled within 48 hours if budget impact > 10%
