# Phase 8 — Cost Observability

![Architecture](../docs/images/phase8-architecture.svg)

## Overview

Phase 8 makes every dollar visible. Engineers can answer: how much does it cost to process one order? Which service consumes 60% of the cluster bill? Where is the waste? What will we spend in 30 days?

Without cost observability, teams provision resources conservatively, never revisit limits, and unknowingly run at 25% CPU utilisation. At $10,000/month cluster spend, that's $7,500/month in unused compute.

---

## What Gets Measured

### Cost per Request
The unit economics metric. Answers: is it getting cheaper or more expensive to serve users as we scale?

```promql
# Cost per 1,000 order-service requests
(
  sum(container_cost_per_hour_dollars{namespace="app", pod=~"order-service.*"})
  / 3600                                    # per second
)
/ sum(rate(order_service_requests_total[5m]))
* 1000
```

If this metric rises without a corresponding increase in request rate, the service is getting less efficient. If it falls as RPS grows, you have good economies of scale.

### Efficiency Ratios
```promql
# CPU efficiency (actual / requested)
# 0.25 = using 25% of what you're asking for → 75% waste
sum(container_cpu_usage_seconds_total{namespace="app"})
/ sum(kube_pod_container_resource_requests{resource="cpu", namespace="app"})

# Memory efficiency
sum(container_memory_working_set_bytes{namespace="app"})
/ sum(kube_pod_container_resource_requests{resource="memory", namespace="app"})
```

Industry average CPU efficiency in Kubernetes: ~15-30%. A well-tuned cluster should be 50-70%.

### 30-Day Cost Forecast
```promql
predict_linear(cost:cluster_total_per_hour:dollars[7d], 30 * 24 * 3600) * 730
```

Linear extrapolation from the last 7 days. Not perfect, but good enough to catch "we doubled our node count and forgot to revisit it."

---

## OpenCost

OpenCost allocates cluster costs to namespaces, deployments, and pods in real time. It reads:
- **AWS pricing API** (via IRSA — no static credentials) for instance costs
- **Kubernetes resource usage** from Prometheus for allocation
- **Spot vs on-demand** — correctly prices spot instances

All data is exposed as Prometheus metrics, consumed by recording rules, and visualised in Grafana.

---

## Rightsizing Engine

`ml/rightsizing/rightsizing_engine.py` — runs weekly, analyses 30 days of actual usage, and generates recommendations.

### Algorithm
```
For each container:
  1. Fetch 30 days of p95 and p99 CPU + memory from Prometheus
  2. Safety checks:
     - Skip if high variance (std/mean > 0.5) → workload is spiky, needs HPA not rightsizing
     - Skip if < 7 days of data → not enough history
  3. Compute recommendations:
     - CPU request  = p95 usage × 1.25 (25% safety margin)
     - CPU limit    = p99 usage × 1.375
     - Memory request = p99 usage × 1.25
     - Memory limit   = p99 usage × 1.5 (memory OOMKill is hard crash — be generous)
  4. Apply guardrail: never recommend cutting > 50% in one step
  5. Compute dollar savings: (current_request - recommended_request) × price_per_unit × 730h
  6. Skip if change is < 10% in either direction (noise threshold)
```

### Output

1. **Prometheus metrics** — `rightsizing_estimated_monthly_savings_dollars` per namespace
2. **Slack digest** — weekly, ranked by dollar savings, with confidence level
3. **kubectl patch script** — `/tmp/rightsizing-patch.sh` for human review and one-click apply

`AUTO_APPLY` is always `false`. A human reviews and applies. The engine recommends; engineers decide.

---

## Cost Alerts

### Budget alerts
- **30-day forecast > $15,000** — fires 30 minutes after detection (allows brief spikes)
- **Hourly spend > 1.5× 24h average** — catches HPA over-scaling or runaway workloads
- **Namespace spend 2× its 7-day average** — catches one team's deployment affecting the bill

### Waste alerts
- **CPU efficiency < 30%** sustained for 2 hours — significant overprovisioning detected
- **PVC > 80% empty and > 10Gi** — unused storage (EBS bills for provisioned, not used)
- **Idle node group** — < 2 app pods per node for 4 hours (autoscaler may be stuck)

### Unit economics alerts
- **Cost per 1k requests doubled** vs 7-day average — traffic may have dropped while costs held
- **ML namespace > $5/hr** — ML training jobs are the likely driver; confirm it's expected

---

## Cost-Aware KEDA Scaling

Standard KEDA scales on workload metrics. Phase 8 adds a **cost ceiling trigger**:

```yaml
- type: prometheus
  metadata:
    query: |
      clamp_min(
        3.00 - sum(container_cost_per_hour_dollars{pod=~"order-service.*"}),
        0
      )
    threshold: "0.50"   # Stop scaling up when < $0.50 headroom under the $3/hr ceiling
```

This prevents runaway scale-out from burning through the monthly budget in a spike. The service scales up to meet demand, but not past the configured cost ceiling without an engineer increasing it.

### Business Hours Scaling
Scale to minimum replicas at 22:00 UTC Monday–Friday (off-hours, low traffic). Resume full KEDA control at 08:00 UTC. Skips scale-down if active RPS > 10 (someone is using it).

### Spot Instances
Stateless services (order, notification, analytics) can tolerate spot interruption:
- `preStop` hook + PodDisruptionBudgets handle 2-minute spot interruption warning
- m5.xlarge spot vs on-demand: ~69% savings
- With 6 app nodes: ~$579/month savings

---

## Files

```
phase8/
├── kubernetes/
│   ├── opencost/
│   │   └── opencost-config.yaml          # OpenCost Helm values + recording rules
│   ├── keda/
│   │   └── cost-aware-scaling.yaml       # ScaledObjects + business hours + spot guidance
│   └── rightsizing/
│       └── rightsizing-deployment.yaml   # Rightsizing engine K8s Deployment
├── ml/
│   └── rightsizing/
│       └── rightsizing_engine.py         # The rightsizing engine itself
├── alerts/
│   └── cost-alert-rules.yaml             # Budget, waste, unit economics alerts
├── dashboards/
│   └── cost-observability.json           # 13-panel Grafana dashboard
└── scripts/
    └── deploy-phase8.sh
```

---

## Deploy

```bash
cd phase8
bash scripts/deploy-phase8.sh

# View recommendations after first weekly run (or trigger manually)
kubectl logs -n ml deployment/rightsizing-engine --tail=50

# View the patch script
kubectl exec -n ml deployment/rightsizing-engine -- cat /tmp/rightsizing-patch.sh

# Apply patches after review
kubectl exec -n ml deployment/rightsizing-engine -- bash /tmp/rightsizing-patch.sh
```

---

## Dashboard Guide

Open `http://grafana/d/cost-observability`. Key panels:

1. **Estimated Monthly Bill** — top-left, should match your AWS bill within 5%
2. **30-Day Forecast** — red if heading over budget
3. **Potential Savings** — bottom line from rightsizing engine
4. **CPU/Memory Efficiency** — below 40% → investigate
5. **Cost per 1,000 Requests** — the unit economics trend
6. **Waste: Unused CPU by Pod** — table sorted by waste, top rows are highest priority
7. **Rightsizing Recommendations** — green = reduce (saves money), red = increase (prevents OOM)

---

## Design Decisions

### Why p95 for requests and p99 for limits?
Requests tell the scheduler how much to *reserve*. Setting them at p95 means 95% of the time there's enough — and HPA handles the remaining 5%. Setting limits at p99 prevents OOMKill from killing pods during brief peaks while still catching runaway processes.

### Why never recommend > 50% reduction in one step?
Aggressive cuts can cause OOMKills or CPU throttling in workloads that occasionally spike. A 50% reduction is already substantial — apply it, monitor for 2 weeks, then apply another reduction if appropriate. Gradual is safer than optimal.

### Why weekly rather than continuous rightsizing?
Resource changes trigger rolling restarts. Rolling restarts during business hours cause brief traffic shifts. Weekly, off-hours adjustments balance accuracy against disruption.

---

## What's Next

[Phase 9–10 →](../phase9-10/README.md) — Scale to 3 active regions and 9 cells with FAANG-level global architecture
