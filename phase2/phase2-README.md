# Phase 2 — SLO Framework & Error Budgets

![Architecture](../docs/images/phase2-architecture.svg)

## Overview

SLOs (Service Level Objectives) are the contract between engineering and the business. This phase implements a full Google SRE Workbook-style SLO framework with multi-window burn rate alerts, error budget tracking, and an executive Grafana dashboard.

**The key insight:** don't alert on symptoms (`error_rate > 0.1%`). Alert on *rate of SLO budget consumption*. That's what burn rate gives you.

---

## Three SLO Tiers

### Tier 1 — User Facing (most strict)
| Service | Availability SLO | Latency SLO |
|---|---|---|
| `order-service` | 99.9% (43.8 min/month budget) | p95 < 200ms |
| `payment-service` | 99.95% (21.9 min/month budget) | p95 < 150ms |

### Tier 2 — Internal Dependencies
| Service | SLO |
|---|---|
| Redis | 99.9% availability |
| Kafka | 99.5% consumer lag below threshold |
| PostgreSQL | 99.9% availability |

### Tier 3 — Infrastructure
| Resource | SLO |
|---|---|
| Node pool | 99% nodes healthy |
| Pod scheduling | 98% success rate |
| Deployment rollouts | 99% success rate |

---

## Multi-Window Burn Rate Alerts

Burn rate measures how fast you are consuming your error budget. A burn rate of 1.0 means you'll exactly exhaust the monthly budget at the current rate. 14.4× means you'll exhaust it in 2 hours.

This implementation uses the **Google SRE Workbook two-window approach** — both a short window (sensitivity) and a long window (specificity). Both must fire to generate an alert. This dramatically reduces false positives.

| Alert | Burn Rate | Short Window | Long Window | Routing | Impact |
|---|---|---|---|---|---|
| 🔴 Critical | 14.4× | 1h | 5m | PagerDuty immediate | Budget gone in 2h |
| 🟠 Warning | 6× | 6h | 30m | PagerDuty ticket | Budget gone in 5 days |
| 🟡 Ticket | 3× | 3d | 6h | GitHub issue | Budget gone in 10 days |
| 🟢 Info | 1× | 30d | — | Slack | Tracking only |

---

## Files

```
phase2/
├── docs/
│   └── slo-definitions.yaml          # Human-readable SLO spec for all services
├── alerts/
│   ├── slo-recording-rules.yaml      # Pre-compute SLO metrics (faster queries)
│   └── slo-burn-rate-alerts.yaml     # Multi-window burn rate PrometheusRules
├── kubernetes/
│   └── observability/
│       ├── helm-values.yaml          # kube-prometheus-stack Helm values
│       └── alertmanager-config.yaml  # Routing: critical → PagerDuty, etc.
├── dashboards/
│   └── slo-executive-overview.json   # Grafana dashboard (import this)
└── deploy-observability.sh           # Install everything
```

---

## Key Recording Rules

Pre-computing SLO metrics as recording rules keeps dashboard queries fast and avoids repeated expensive range queries.

```yaml
# Error rate over 5-minute window (used in burn rate calculation)
- record: slo:order_service_availability:error_rate5m
  expr: |
    sum(rate(order_service_requests_total{status_code=~"5.."}[5m]))
    / sum(rate(order_service_requests_total[5m]))

# Error budget remaining (0.0 to 1.0, where 0 = budget exhausted)
- record: slo:order_service_availability:error_budget_remaining
  expr: |
    1 - (
      sum(increase(order_service_requests_total{status_code=~"5.."}[30d]))
      / sum(increase(order_service_requests_total[30d]))
    ) / (1 - 0.999)  # 1 - SLO target
```

---

## Error Budget Dashboard

The executive overview dashboard shows:

- **Error budget remaining** — as a gauge, red when < 20%
- **Burn rate** — current rate of consumption per service
- **Budget consumed this month** — rolling 30-day window
- **Projected depletion date** — if burn rate holds, when does budget run out?
- **Multi-window burn rate heatmap** — visualise which time windows are firing

---

## Deploy

```bash
cd phase2
bash deploy-observability.sh

# This installs:
# - kube-prometheus-stack (Prometheus + Grafana + Alertmanager)
# - Loki stack
# - Tempo
# - Applies recording rules and burn rate alerts
# - Imports the executive overview dashboard
```

---

## Verify SLOs Are Working

```bash
# Port-forward Prometheus
kubectl port-forward -n observability svc/kube-prometheus-stack-prometheus 9090:9090

# Check recording rules are computing
curl -s 'localhost:9090/api/v1/query?query=slo:order_service_availability:error_budget_remaining'

# Check burn rate alert rules are loaded
curl -s 'localhost:9090/api/v1/rules' | jq '.data.groups[] | select(.name | contains("slo"))'
```

---

## Design Decisions

### Why multi-window over single-window alerts?
Single-window burn rate alerts generate too many false positives for transient spikes. The two-window approach requires the burn rate to be elevated in *both* a short window (is it happening now?) and a long window (has it been elevated for a while?). This filters out 1-minute anomalies that don't actually threaten the budget.

### Why 99.95% for payment vs 99.9% for orders?
Payment failures are immediate, visible, and revenue-impacting. An order failure can be retried; a payment failure erodes user trust directly. The tighter SLO reflects the higher customer impact — and forces us to invest more in payment-service reliability.

### Why pre-compute as recording rules?
Grafana dashboards with 30-day range queries over raw metrics are slow and expensive. Recording rules pre-compute the result on the Prometheus side every minute, so dashboard loads are fast even at scale.

---

## What's Next

[Phase 3 →](../phase3/README.md) — Build the alerting and incident management system that acts on these SLO alerts
