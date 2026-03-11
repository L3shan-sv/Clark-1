# docs/gameday-runbook-template.md
# Game Day Runbook — Chaos Experiment Record
# Copy this file and fill in for each experiment session.

---

## Session Details

| Field | Value |
|---|---|
| **Date** | YYYY-MM-DD |
| **Experiment** | Exp 01 Pod Kill / Exp 02 Network / Exp 03 Memory / Exp 04 Node / Exp 05 Cascade |
| **Chaos Engineer** | @name |
| **Observer(s)** | @name, @name |
| **Environment** | staging / production |
| **Pre-experiment error budget** | XX% remaining |

---

## Pre-Chaos Checklist

Run these BEFORE applying any chaos:

```bash
# 1. All pods healthy
kubectl get pods -n app
kubectl get pods -n ml

# 2. No active alerts
kubectl port-forward -n observability svc/alertmanager 9093:9093
# Open http://localhost:9093 — should show 0 active alerts

# 3. Error budget check
kubectl port-forward -n observability svc/prometheus 9090:9090
# Query: slo:order_service_availability:error_budget_remaining
# Must be > 0.30 (30%) to proceed

# 4. Argo Workflows controller running
kubectl get pods -n argo

# 5. Screenshot current dashboards
# Open: http://grafana/d/slo-executive-overview
# Open: http://grafana/d/service-health-red
# Take screenshots — you'll compare these to post-chaos state
```

**Pre-chaos screenshots taken:** ☐ Yes ☐ No

---

## Hypothesis

> We believe that when **[describe the failure condition]** occurs,
> the system will **[describe expected behaviour]** within **[time window]**,
> and the SLO will **[maintain / breach by X%]**.
>
> We will verify this by observing **[specific metric]** in Grafana
> and confirming **[specific workflow/alert]** triggered.

**Written hypothesis:**

___

---

## Experiment Execution

```bash
# Apply the chaos experiment
kubectl apply -f kubernetes/chaos-mesh/experiments/0X-experiment-name.yaml

# Monitor in real time
watch kubectl get chaosexperiment -n chaos
```

**Chaos applied at:** HH:MM UTC

---

## Observations (fill in real-time)

| Time | What happened |
|---|---|
| T+00:00 | Chaos applied |
| T+00:XX | First alert fired: ___________ |
| T+00:XX | Auto-remediation triggered: ___________ |
| T+00:XX | Error rate peaked at: ___________ |
| T+00:XX | System began recovering |
| T+00:XX | Steady state restored |

**Time to Detect (TTD):** ___ minutes
**Time to Remediate (TTR):** ___ minutes
**Error budget consumed:** ___ minutes
**Manual intervention required:** ☐ Yes ☐ No

---

## Hypothesis vs Reality

| Expectation | Actual | Pass/Fail |
|---|---|---|
| Alert fired within 2 min | Fired at T+___ | ✅ / ❌ |
| Auto-remediation triggered | ☐ Yes ☐ No | ✅ / ❌ |
| Recovery within X minutes | Recovered at T+___ | ✅ / ❌ |
| SLO maintained | Budget used: ___ min | ✅ / ❌ |
| No manual intervention | ☐ Yes ☐ No | ✅ / ❌ |

---

## What Surprised Us

_What was different from the hypothesis? Even small surprises are worth noting._

___

---

## Action Items

| # | Finding | Action | Owner | Due |
|---|---|---|---|---|
| 1 | | | | |
| 2 | | | | |

---

## Post-Chaos Cleanup

```bash
# Delete the chaos experiment (stops chaos immediately if still running)
kubectl delete -f kubernetes/chaos-mesh/experiments/0X-experiment-name.yaml

# Verify all pods back to healthy
kubectl get pods -n app
kubectl rollout status deployment/order-service -n app

# Confirm error rate back to 0
# Prometheus: sum(rate(order_service_requests_total{status_code=~"5.."}[5m]))
```

**Post-chaos state:** All pods healthy ☐ | All alerts resolved ☐ | Error rate 0 ☐

---

## Graduation Criteria Assessment

- [ ] Correct alert fired within 2 minutes
- [ ] Auto-remediation triggered without human action
- [ ] System recovered within SLO budget
- [ ] No data loss
- [ ] No manual intervention required

**Overall result:** ✅ PASSED / ❌ FAILED / ⚠️ PARTIAL

**Signed off by:** ___________ | **Date:** ___________
