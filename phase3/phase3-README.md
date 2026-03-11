# Phase 3 — Alerting & Incident Management

![Architecture](../docs/images/phase3-architecture.svg)

## Overview

Phase 3 transforms raw Prometheus alerts into a structured incident management system. Every alert has a runbook. Every runbook has a decision tree. Every incident feeds a postmortem. And Alertmanager's inhibition rules ensure that when Redis goes down, you don't get 47 downstream alerts — just one for the root cause.

---

## Alert Pipeline

```
Prometheus Rule fires
    ↓
Alertmanager (deduplication, grouping, inhibition)
    ↓
Route: severity × team × service
    ↓
critical  → PagerDuty + Slack #incidents
warning   → Slack #alerts
info      → Slack #monitoring
    ↓
Grafana OnCall (schedule, escalation, acknowledge)
    ↓
On-call engineer receives alert with runbook link
```

---

## Inhibition Rules

Inhibition prevents alert storms. If a root cause alert is firing, silence all downstream symptoms.

```yaml
# If Redis is down (source), silence all consumer-related alerts (target)
- source_match:
    alertname: RedisDown
  target_match_re:
    alertname: (KafkaConsumerLag|NotificationServiceErrors|AnalyticsServiceErrors)

# If a node is NotReady, silence pod alerts on that node
- source_match:
    alertname: NodeNotReady
  target_match:
    alertname: PodCrashLooping
  equal: [node]
```

Without inhibition: Redis goes down → 15 alerts fire → on-call engineer spends 10 minutes figuring out which one to look at first. With inhibition: 1 alert fires (RedisDown) with the correct runbook.

---

## The Six Runbooks

Each runbook follows a **decision-tree format**: start at the top, follow the branch that matches what you observe, and arrive at a concrete action.

### `high-error-rate.md`
Triggered by: `ErrorRateCritical` (> 1% for 5 min)
```
Is it one service or all services?
├── One service → check recent deployments → rollback if within 30 min
├── One service → check upstream dependencies → follow dependency runbook
└── All services → check infrastructure layer → escalate to platform team
```
Contains: PromQL queries, LogQL queries, rollback command, escalation timeline.

### `high-latency.md`
Triggered by: `LatencyP95Critical` (p95 > 500ms for 5 min)
```
Check: is it latency or timeouts?
├── Latency → check DB query times, Redis latency, downstream services
└── Timeouts → check connection pool exhaustion, HPA, resource limits
```

### `kafka-consumer-lag.md`
Triggered by: `KafkaConsumerLagHigh` (> 30,000 messages for 10 min)
```
Is the consumer running?
├── No → restart notification-service
└── Yes → is it processing? Check throughput metrics
    ├── Processing slowly → check downstream dependencies (email/SMS API)
    └── Stuck → check for poison messages → skip or DLQ
```

### `redis-memory-pressure.md`
Triggered by: `RedisMemoryHigh` (> 85% for 5 min)
```
Check eviction policy → switch to allkeys-lru if not set
Check largest keys → find and remove or TTL-cap oversized keys
Check HPA → scale out services to distribute cache load
```

### `node-disk-pressure.md`
Triggered by: `NodeDiskPressure`
```
Identify high-usage paths → /var/log (rotate), /var/lib/containers (prune)
Check PVC usage → identify growing PVCs
Cordon node → drain → add storage or replace node
```

### `security-alert.md`
Triggered by: `FalcoCriticalSecurityEvent`
```
Identify affected pod → capture forensics (logs, network connections)
Isolate: NetworkPolicy deny-all for pod
Escalate to security team immediately
Do NOT attempt to remediate without security team sign-off
```

---

## Infrastructure Alerts

`alerts/infrastructure-alerts.yaml` contains:

| Alert | Threshold | Severity |
|---|---|---|
| `PodCrashLooping` | > 5 restarts in 15 min | warning |
| `PodOOMKilled` | Any OOMKill event | warning |
| `NodeNotReady` | Node not ready > 5 min | critical |
| `NodeDiskPressure` | > 85% disk | warning |
| `RedisMemoryHigh` | > 85% maxmemory | warning |
| `HPAMaxReplicasReached` | HPA at max replicas | warning |
| `DeploymentReplicasMismatch` | Desired ≠ available > 10 min | warning |
| `KafkaConsumerLagHigh` | Lag > 30,000 messages | warning |

---

## Postmortem Template

Every P0/P1 incident produces a postmortem within 48 hours. The template (`docs/postmortems/postmortem-template.md`) enforces:

- **Blameless** — focus on systems and processes, not people
- **Timeline** — when was it detected, when was it mitigated, when was it resolved?
- **Root cause** — what actually caused it (not what you initially thought)?
- **Contributing factors** — what made it worse?
- **Action items** — specific, assigned, time-boxed

---

## Files

```
phase3/
├── alerts/
│   └── infrastructure-alerts.yaml    # All infrastructure PrometheusRules
├── dashboards/
│   └── service-health-red.json       # RED dashboard (Grafana import)
├── docs/
│   ├── runbooks/
│   │   ├── high-error-rate.md
│   │   ├── high-latency.md
│   │   ├── kafka-consumer-lag.md
│   │   ├── redis-memory-pressure.md
│   │   ├── node-disk-pressure.md
│   │   └── security-alert.md
│   └── postmortems/
│       └── postmortem-template.md
└── kubernetes/
    └── alerting/
        └── grafana-oncall.yaml       # OnCall deployment + schedule config
```

---

## RED Dashboard

The Service Health RED dashboard provides at-a-glance status for every service:

- **Rate** — requests per second (is traffic normal?)
- **Errors** — 4xx and 5xx rates (is it broken?)
- **Duration** — p50, p95, p99 latency (is it slow?)
- **Saturation** — CPU, memory, queue depth (is it about to break?)
- **SLO burn rate** — current rate of error budget consumption

---

## Design Decisions

### Why Grafana OnCall over PagerDuty directly?
OnCall is the scheduler and escalation manager. PagerDuty is a possible destination. OnCall lets you define complex rotation schedules, override rules, and escalation chains — all in the same Grafana instance where you already live. It also integrates directly with the dashboards, so the on-call engineer lands on the right panel when they click the alert link.

### Why decision-tree runbooks over prose?
At 3am, nobody reads prose. Decision trees force the author to think through every branch up front. The on-call engineer just follows the tree — no cognitive load about what to check next.

### Why inhibition rules?
Without inhibition, a single infrastructure failure generates N downstream alerts. The engineer spends cognitive energy triaging the alert list instead of fixing the root cause. Inhibition collapses the noise to one actionable alert.

---

## What's Next

[Phase 4 →](../phase4/README.md) — Automate the runbook actions so Argo Workflows executes them before the on-call engineer even wakes up
