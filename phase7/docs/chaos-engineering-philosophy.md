# docs/chaos-engineering-philosophy.md
#
# Chaos Engineering at the Autonomous Observability Platform
#
# "Hope is not a strategy. Neither is assuming your system works."
#
# We run chaos experiments to answer one question:
#   "Does the system actually behave the way we THINK it does?"
#
# The answer is almost always "no, not quite" — and that's the point.

## Why We Do This

Traditional testing proves code works in isolation.
Chaos engineering proves the SYSTEM holds under real failure conditions.

We have:
  - Self-healing workflows (Phase 4)
  - ML anomaly detection (Phase 5)
  - mTLS and zero trust (Phase 6)
  - SLO burn rate alerts (Phase 2)

But do they actually work TOGETHER when something breaks at 3am?
Chaos engineering is how we find out before our users do.

## The Five Experiments

| # | Experiment | What We're Testing |
|---|---|---|
| 1 | Pod Kill — order-service | Auto-restart, traffic rerouting, alert fires correctly |
| 2 | Network Partition | Order → Payment latency, circuit breaker, SLO burn rate |
| 3 | Memory Pressure | OOM detection, Redis eviction, HPA response |
| 4 | Node Failure | Pod rescheduling, PodDisruptionBudget, multi-AZ resilience |
| 5 | Cascade Failure | Full dependency chain: Redis down → errors → rollback |

## Blast Radius Control

Every experiment has:
  - **Namespace selector** — only affects `app` namespace
  - **Label selector** — only specific pods, not all
  - **Duration limit** — auto-terminates after N minutes
  - **Abort condition** — paused if SLO budget < 20%
  - **Rollback** — `kubectl delete chaosexperiment` stops immediately

## Game Day Process

1. **Pre-chaos:** Screenshot current dashboards. Note baseline SLO budget.
2. **Hypothesis:** Write down what you EXPECT to happen. Be specific.
3. **Blast radius:** Confirm selectors before applying. Start small.
4. **Apply chaos:** `kubectl apply -f experiment.yaml`
5. **Observe:** Watch Grafana. Did alerts fire? Did auto-remediation trigger?
6. **Measure:** Did SLO breach? How long until recovery? TTD/TTR?
7. **Post-chaos:** Compare to hypothesis. Document what was different.
8. **Action items:** Every gap between hypothesis and reality = ticket.

## Hypothesis Template

```
We believe that when [failure condition] occurs,
the system will [expected behaviour] within [time window],
and the SLO will [maintain/breach by X%].

We will verify this by observing [specific metric/alert] in Grafana
and confirming [specific auto-remediation step] triggered in Argo.
```

## Steady State Indicators

Before running ANY experiment, confirm these are healthy:
  - All pods Running and Ready
  - Error rate < 0.01% for last 30 minutes
  - Error budget > 30% remaining
  - All ML models trained (no drift alerts)
  - Argo Workflows controller running

DO NOT run chaos if any of these are failing.
You can't learn from chaos if the system is already broken.

## Graduation Criteria

A chaos experiment "passes" when:
  ✅ Correct alert fired within 2 minutes of fault injection
  ✅ Auto-remediation triggered (or correct runbook was followed)
  ✅ System recovered to steady state within SLO budget
  ✅ No data loss occurred
  ✅ Users (if any) experienced < SLO threshold of errors
