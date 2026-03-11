# Phase 9–10 — FAANG Scale: Multi-Region & Cell Architecture

![Architecture](../docs/images/phase9-10-architecture.svg)

## Overview

Phases 9 and 10 take everything built in phases 0–8 and deploy it across three AWS regions, nine cells, with anycast global routing and sub-second failover. This is the architecture that handles Black Friday traffic, survives a full AZ outage without anyone noticing, and keeps TTFB under 50ms globally.

**The two key ideas:**
1. **Active-active multi-region** — all three regions serve traffic simultaneously. No cold standby, no failover delay.
2. **Cell architecture** — each region is divided into cells. A cell failure affects ~11% of global traffic, not 100%.

---

## Multi-Region Architecture

### Why Active-Active Over Active-Passive?

| | Active-Passive | Active-Active |
|---|---|---|
| **Failover time** | 2–5 minutes (DNS TTL + cold start) | < 1 second (anycast) |
| **Capacity utilisation** | 50% (standby sits idle) | 100% |
| **Monthly cost** | 2× (pay for idle region) | Same (all regions used) |
| **Latency** | Worse (all traffic to one region) | Better (serve from nearest) |

### Traffic Distribution
```
AWS Global Accelerator (anycast IPs)
    │
    ├── us-east-1   ←── 40% of global traffic
    ├── us-west-2   ←── 40% of global traffic
    └── eu-west-1   ←── 20% of global traffic (GDPR region)
```

Global Accelerator uses **anycast** — the same IP address announces from multiple AWS PoPs worldwide. Users connect to the geographically nearest PoP, which routes to the lowest-latency healthy region. No DNS TTL to wait on.

### Health Checks
Route53 checks each region every 10 seconds. Two consecutive failures → region is removed from routing. With anycast, traffic shifts in under 30 seconds.

---

## Regional Stack (Identical × 3)

Each region is a complete, independent deployment:

| Component | Configuration |
|---|---|
| **EKS** | 1.29, 3 node groups (system/app/observability) |
| **VPC** | Non-overlapping CIDRs (`10.10.x`, `10.20.x`, `10.30.x`) for peering |
| **MSK Kafka** | 3 brokers (1 per AZ), TLS, 500GiB per broker |
| **ElastiCache Redis** | 1 primary + 2 replicas, Multi-AZ, TLS |
| **Aurora** | Global cluster: writer in us-east-1, read replicas in us-west-2 + eu-west-1 |

### Cross-Region Shared State
Some state must be consistent across all regions. DynamoDB Global Tables handle this:

| Table | Purpose | Consistency |
|---|---|---|
| `order-idempotency-keys` | Prevent duplicate orders | Eventually consistent (< 1s lag) |
| `distributed-rate-limits` | Per-customer rate limits | Eventually consistent |

Everything else is regional — Kafka topics, Redis caches, and application state stay in-region. This keeps latency low and prevents cross-region coupling.

---

## Cell Architecture

Within each region, three cells:

```
Region: us-east-1
    ├── cell-0  (serves customers hashed to 0,3,6)
    ├── cell-1  (serves customers hashed to 1,4,7)
    └── cell-2  (serves customers hashed to 2,5,8)
```

### Customer Routing

```lua
-- Envoy Lua filter (runs on every inbound request)
local hash = 5381
for i = 1, #customer_id do
    hash = ((hash * 33) + string.byte(customer_id, i)) % 2147483647
end
local cell = CELLS[(hash % NUM_CELLS) + 1]   -- cell-0, cell-1, or cell-2
```

The same customer **always** hits the same cell. This means:
- Session state is always local to that cell
- Debugging is easy (filter by cell_id)
- Canary deployments are natural (deploy to cell-0, verify, roll out)

### Blast Radius
```
Cell failure:   1/3 of regional traffic = 1/3 × 40% = ~13% of global
Region failure: 40% of global traffic (immediately redistributed by Global Accelerator)
Worst case (1 cell, 1 region): ~13% of users see errors for < 30 seconds
```

Compare to a non-cell architecture: a bad deploy to us-east-1 takes down 40% of global traffic until rollback completes (3–5 minutes).

### Cell Isolation
Each cell is a Kubernetes namespace with:
- Its own copy of all Deployments and Services
- A `ResourceQuota` (prevents one cell consuming all node resources)
- Independent HPA (can scale independently)
- Independent deployment pipeline (deploy to cell-0, monitor, proceed)

---

## Adaptive Traffic Shaping

### Circuit Breakers (Istio)
```
order-service circuit breaker:
  maxConnections: 100
  http1MaxPendingRequests: 100      # Queue depth — beyond this → instant 503
  consecutiveGatewayErrors: 5       # 5 consecutive 5xx → eject pod from load balancer
  baseEjectionTime: 30s             # Ejected pod stays out for minimum 30 seconds
  maxEjectionPercent: 50            # Never eject more than half the pods
```

### Priority-Based Load Shedding
Under load, drop the least important requests first:

| Priority | Endpoints | Shed when CPU > |
|---|---|---|
| P0 | `/health`, `/payments` | Never |
| P1 | `POST /orders`, `GET /orders` | 95% |
| P2 | `/notifications` | 90% |
| P3 | Batch, analytics queries | 80% |

An overloaded system that sheds P3 requests protects P0/P1 revenue-critical flows. An overloaded system with no shedding collapses everything simultaneously.

### KEDA Predictive Scaling
Four triggers per service, combined:
1. **RPS** — scale when current requests per pod > 50
2. **p95 latency** — scale when latency > 180ms (before SLO breach at 200ms)
3. **Cost ceiling** — stop scaling when hourly cost approaches limit
4. **ML forecast** — pre-scale based on Prophet's 30-min-ahead prediction

---

## Capacity Planning

`ml/capacity-planning/capacity_planner.py` — runs weekly.

```
For each service + resource (CPU, memory):
  1. Fetch 30 days of usage from Prometheus
  2. Fit linear trend (slope + intercept)
  3. Extrapolate to 30-day and 90-day points
  4. Calculate "breach day" — when usage hits 85% of capacity
  5. Assign urgency: critical (< 14d), urgent (< 30d), plan (< 60d), ok
  6. Generate recommendation: "Add 2 nodes in the next 9 days"

Weekly Slack digest + Prometheus metrics for Grafana
```

---

## Toil Budget Tracker

Google SRE defines toil as repetitive, manual, operational work that scales linearly with traffic and produces no lasting value. Policy: **toil must consume < 50% of SRE time.**

The tracker measures weekly:

| Category | How measured |
|---|---|
| Incident response | Pages that required human action (total − auto-remediated) |
| On-call overhead | Non-actionable pages (false positives) × 15 min each |
| Manual deployments | Deploys not via Argo CD × 30 min each |
| Ticket toil | Operational tickets (JIRA/Linear API) |

If total toil > 50% of `SRE_TEAM_SIZE × 40 hours/week`, a Slack alert fires identifying the top contributor. This is the signal to automate.

---

## Files

```
phase9-10/
├── terraform/
│   ├── global/
│   │   └── main.tf                       # Global Accelerator, DynamoDB Global Tables, Route53
│   └── regions/
│       └── us-east-1/
│           └── main.tf                   # Regional EKS, MSK, Redis, Aurora (deploy × 3)
├── kubernetes/
│   ├── cell-architecture/
│   │   └── cell-router.yaml              # Envoy filter, VirtualServices, cell namespaces, ResourceQuotas
│   └── traffic-shaping/
│       └── adaptive-traffic-shaping.yaml # Circuit breakers, load shedding, KEDA ScaledObjects, rate limiting
├── ml/
│   └── capacity-planning/
│       └── capacity_planner.py           # Capacity forecasting + toil budget tracking
├── dashboards/
│   └── global-command-center.json        # 15-panel Grafana: global health, cells, SLO, toil, cost, security
├── docs/
│   └── architecture-decision-records.md  # 7 ADRs with full reasoning
└── scripts/
    └── deploy-phase9-10.sh               # Orchestrates all regional + global deploys
```

---

## Deploy

```bash
cd phase9-10

# Prerequisites: all phases 0-8 deployed in at least us-east-1

# 1. Deploy global infrastructure (once)
cd terraform/global
terraform init && terraform apply

# 2. Deploy regional stacks (parallel)
for region in us-east-1 us-west-2 eu-west-1; do
  cd ../regions/$region
  terraform init && terraform apply &
done
wait

# 3. Configure cell routing + traffic shaping
for region in us-east-1 us-west-2 eu-west-1; do
  kubectl config use-context $region
  kubectl apply -f ../../kubernetes/cell-architecture/
  kubectl apply -f ../../kubernetes/traffic-shaping/
done

# Or run the full orchestrator:
bash scripts/deploy-phase9-10.sh
```

---

## Verifying Multi-Region Health

```bash
# Traffic distribution (should be ~40/40/20)
kubectl port-forward -n observability svc/prometheus 9090:9090
# Query: sum by (region) (rate(order_service_requests_total[5m]))

# Cell health (should be balanced)
# Query: cell:error_rate:ratio

# Force a failover test (change traffic dial to 0% for one region)
aws globalaccelerator update-endpoint-group \
  --endpoint-group-arn <arn> \
  --traffic-dial-percentage 0   # Instantly removes region from routing

# Restore
aws globalaccelerator update-endpoint-group \
  --endpoint-group-arn <arn> \
  --traffic-dial-percentage 40
```

---

## Architecture Decision Records

Seven key decisions documented with full reasoning in `docs/architecture-decision-records.md`:

1. Active-active vs active-passive
2. Cell architecture vs pure horizontal scaling
3. Thompson Sampling vs Q-learning
4. Granger causality vs correlation for RCA
5. Falco eBPF vs kernel module
6. Prophet vs pure LSTM for forecasting
7. OPA Gatekeeper vs Kyverno

---

## What Now?

The platform is complete. Here's what runs without human involvement:

- Detects anomalies within 60 seconds
- Identifies root cause within 90 seconds
- Executes remediation via Argo Workflows
- Scales predictively 30 minutes ahead of traffic
- Sheds low-priority load under pressure
- Enforces zero trust at every layer
- Validates itself with chaos experiments weekly
- Rightsizes resources and tracks toil budget
- Fails over transparently between 9 cells across 3 regions

What humans do:
- Approve RL agent graduation (shadow → auto)
- Approve the monthly cascade chaos experiment
- Review weekly cost + toil reports
- Approve capacity increases beyond 50% change
