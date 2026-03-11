# Phase 4 — Self-Healing Automation

![Architecture](../docs/images/phase4-architecture.svg)

## Overview

Phase 4 automates the runbooks from Phase 3. When Prometheus fires an alert, instead of paging a human to follow a checklist, an Argo Workflow executes that checklist automatically — with full audit logging at every step.

**The pipeline:**
```
Prometheus alert → Alertmanager webhook → Argo EventSource → Argo Sensor → Workflow → Slack + audit log
```

Humans are notified of what happened. They are not the ones doing it.

---

## Architecture

### Argo Events
Sits between Alertmanager and Argo Workflows. Receives webhook POST from Alertmanager, filters on alert name and severity, and triggers the correct Sensor.

```
Alertmanager
    │  POST /webhooks/alertmanager
    ▼
EventSource (alertmanager-webhook)
    │  Filters: alertname, severity, labels
    ▼
Sensor (remediation-sensors)
    │  Matches event → triggers workflow
    ▼
Argo Workflow Template
```

### 5 Webhook Endpoints

| Endpoint | Trigger Condition | Workflow Called |
|---|---|---|
| `/pod-crashlooping` | PodCrashLooping alert fires | `pod-crashloop-remediation` |
| `/deployment-error-spike` | ErrorRateCritical + recent deploy | `deployment-auto-rollback` |
| `/redis-memory-high` | RedisMemoryHigh alert fires | `redis-and-scale-remediation` |
| `/request-rate-surge` | RequestRateSurge fires | `redis-and-scale-remediation` (scale path) |
| `/anomaly-detected` | ML anomaly detector POSTs | RL agent selects workflow (Phase 5) |

---

## The Three Workflow Templates

### `pod-crashloop-remediation` — 7-step DAG

```
Step 1: verify-crash-loop      → Confirm pod is actually crashing (not stale alert)
Step 2: capture-logs           → kubectl logs --previous → store in audit artifact
Step 3: check-recent-deploys   → Was there a deploy in the last 30 minutes?
    ├── Yes → trigger deployment-auto-rollback
    └── No → continue
Step 4: cordon-node            → kubectl cordon <node> (no new pods scheduled)
Step 5: drain-node             → kubectl drain <node> --grace-period=60
Step 6: verify-recovery        → Wait 90s, check error rate < 0.1%
Step 7: audit-log              → Record action, outcome, SLO impact to Elasticsearch
```

### `deployment-auto-rollback` — 5-step

```
Step 1: confirm-error-rate     → Error rate must be > 1% for 3 consecutive minutes
Step 2: capture-deploy-history → kubectl rollout history → store previous revision
Step 3: rollout-undo           → kubectl rollout undo deployment/<name>
Step 4: verify-recovery        → Poll error rate every 30s for 5 minutes
Step 5: audit + notify         → Slack: "Auto-rollback executed for <service> at <time>"
```

### `redis-and-scale-remediation` — branching

```
Step 1: check-eviction-policy  → redis-cli CONFIG GET maxmemory-policy
    ├── Not allkeys-lru → redis-cli CONFIG SET maxmemory-policy allkeys-lru
    └── Already set → proceed to step 2
Step 2: predictive-scale       → Call ML forecaster API → adjust HPA min replicas
Step 3: verify-memory          → Check Redis memory < 80% after 60s
Step 4: audit + notify
```

---

## Files

```
phase4/
├── kubernetes/
│   ├── argo-events/
│   │   ├── install.yaml                          # Argo Events CRDs + controller
│   │   ├── event-sources/
│   │   │   └── alertmanager-webhook.yaml         # EventSource: HTTP webhook server
│   │   └── sensors/
│   │       └── remediation-sensors.yaml          # Sensors: event → workflow trigger
│   └── argo-workflows/
│       ├── templates/
│       │   ├── pod-crashloop-remediation.yaml
│       │   ├── deployment-auto-rollback.yaml
│       │   └── redis-and-scale-remediation.yaml
│       └── workflows/
│           └── test-workflows.yaml               # Manual trigger for testing
└── scripts/
    └── deploy-phase4.sh
```

---

## Deploy

```bash
cd phase4
bash scripts/deploy-phase4.sh

# This installs:
# - Argo Events controller + CRDs
# - Argo Workflows controller
# - EventSource (starts HTTP webhook server)
# - Sensors (wires events to workflow templates)
# - WorkflowTemplates
```

---

## Testing

```bash
# Test pod crashloop remediation manually
kubectl create -f kubernetes/argo-workflows/workflows/test-workflows.yaml

# Watch workflow progress
kubectl get workflows -n argo -w
argo watch <workflow-name> -n argo

# Test by actually triggering an alert
kubectl port-forward -n argo svc/argo-events-eventsource-svc 12000:12000
curl -X POST http://localhost:12000/pod-crashlooping \
  -H 'Content-Type: application/json' \
  -d '{"alerts":[{"labels":{"alertname":"PodCrashLooping","pod":"order-service-xxx","namespace":"app"}}]}'
```

---

## Audit Trail

Every workflow execution writes an audit record:

```json
{
  "timestamp": "2024-01-15T03:42:11Z",
  "incident_id": "INC-2024-0042",
  "trigger": "PodCrashLooping",
  "affected": {"pod": "order-service-7d9f8b-xxx", "namespace": "app"},
  "workflow": "pod-crashloop-remediation",
  "steps_completed": ["verify", "capture-logs", "cordon", "drain", "verify-recovery"],
  "outcome": "SUCCESS",
  "duration_seconds": 142,
  "slo_impact_minutes": 0.4,
  "human_intervention": false,
  "slack_thread": "https://..."
}
```

---

## When Automation Fails

If a workflow fails after 3 retries, it escalates:
1. PagerDuty critical alert fires with full context
2. Slack message includes: what was tried, what failed, current system state
3. Runbook link in the alert takes the on-call engineer to the correct Phase 3 runbook
4. The workflow artifacts (logs captured in step 2) are attached

---

## Design Decisions

### Why Argo Events + Argo Workflows over a custom operator?
Argo Events handles the event-to-action wiring with built-in retry, exactly-once semantics, and rich filtering. Argo Workflows provides a DAG execution engine with artifact storage, step retry, and timeout handling. Writing a custom operator would duplicate all of this. The Argo ecosystem is the CNCF standard for this problem.

### Why not just run kubectl commands in a shell script?
Shell scripts have no retry logic, no artifact storage, no DAG execution, and no audit trail. When a rollback fails at 3am, you need to know *which step* failed and *why*. Argo Workflows shows you exactly that in a visual DAG.

### Why confirm before acting?
Step 1 in every workflow re-confirms the condition before acting. Alerts can be stale (the issue resolved itself in the 30 seconds between alert firing and webhook delivery). Confirming before acting prevents unnecessary remediations.

---

## What's Next

[Phase 5 →](../phase5/README.md) — Add ML intelligence to *detect* incidents before they breach SLOs, not just react after the alert fires
