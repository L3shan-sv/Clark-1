# Phase 7 — Chaos Engineering

![Architecture](../docs/images/phase7-architecture.svg)

## Overview

Phase 7 answers the question every engineer should ask but rarely does: **does the system actually behave the way we think it does?**

We have self-healing workflows, ML anomaly detection, mTLS, and SLO alerts. But do they work *together* when something breaks at 3am? Chaos engineering is how we find out before our users do.

> "Hope is not a strategy. Neither is assuming your system works."

---

## The Five Experiments

Ordered from smallest to largest blast radius. Always start with Experiment 1.

### Experiment 1 — Pod Kill

**What:** Kill one of the three order-service pods.

**Hypothesis:** The remaining two pods absorb traffic within 10 seconds. Kubernetes reschedules a replacement within 60 seconds. Error rate blips < 0.1% for < 30 seconds. The `PodCrashLooping` alert does **not** fire (single kill ≠ crash loop).

**What this tests:**
- PodDisruptionBudget (configured: `minAvailable: 2`) prevents killing all pods
- Readiness probe routes traffic away from the terminating pod before it dies
- `preStop: sleep 5` drains in-flight requests before shutdown
- Kubernetes scheduler places the new pod in a different AZ (topology spread)

**Variant 1b:** Pod failure (OOMKill simulation) — tests that Falco fires the OOMKill alert and the pod restarts cleanly without data corruption.

---

### Experiment 2 — Network Partition

**What:** Three escalating phases:
- 2a: 500ms latency with 50% jitter on payment-service ingress (5 minutes)
- 2b: Full network partition between order-service and payment-service (3 minutes)
- 2c: 30% packet loss across all app services (5 minutes)

**Hypothesis:** For 2b — order-service returns 503s within 10 seconds. Error rate SLO alert fires within 2 minutes. Alertmanager inhibition fires only the root cause alert (payment-service unreachable), not 15 downstream symptom alerts. Causal inference engine identifies payment-service as the upstream cause within 90 seconds.

**Why three variants?** Real network problems start as latency (2a), escalate to partial failures (2c), and occasionally become full partitions (2b). Testing only the full partition misses the "slow degradation" failure mode that's actually harder to detect.

---

### Experiment 3 — Memory Stress

**What:** Three intensities applied to order-service pods:
- 3a: 75% of memory limit (controlled stress — tests monitoring without triggering OOMKill)
- 3b: 200% of memory limit (guaranteed OOMKill within 60 seconds)
- 3c: 80% CPU across **all** order-service pods simultaneously (forces HPA to act)

**Hypothesis for 3c:** HPA scales from 3 → 5 pods within 90 seconds. p95 latency stays below 200ms (SLO threshold). CPU efficiency metric rises (more pods = more available capacity per pod).

**Key insight for 3c:** Applying stress to *all* pods simultaneously forces HPA to scale out, rather than just redistributing load. This tests whether HPA is fast enough to prevent SLO breach under a correlated load spike.

---

### Experiment 4 — Node Failure

**What:** Network-isolate one EKS node (simulating an AZ network failure). The node stays "running" but cannot communicate.

**Hypothesis:** Node enters `NotReady` within 40 seconds. Pods are evicted within 5 minutes (default `node.kubernetes.io/unreachable` toleration period). Pods reschedule onto healthy nodes. Cluster autoscaler provisions a replacement node within 5 minutes. SLO budget impact < 1 minute.

**What this uniquely tests:** Whether topology spread constraints are *actually* spreading pods across AZs. Many teams configure topology spread but never verify it works. This experiment proves it.

---

### Experiment 5 — Cascade Failure (The Boss Fight)

**What:** Four failure types injected with 30-second staggered timing, orchestrated by an Argo Workflow:

```
T+0:00  Redis pod fails (all cache misses, DB load spikes)
T+0:30  notification-service pod fails (Kafka consumer lag grows)
T+1:00  200ms latency on payment-service (cache-miss side effect)
T+1:30  CPU stress on order-service pods (coincident traffic pattern)
```

**Hypothesis:** Redis eviction workflow fires at T+0:30 (auto-remediation). Causal inference identifies Redis as root cause (not order-service which shows visible errors). No manual intervention required. Full recovery within 10 minutes of chaos termination. SLO budget consumed < 5 minutes total.

**What this uniquely tests:**
- Do remediation workflows conflict with each other when multiple fire simultaneously?
- Does the causal engine correctly identify the *upstream* root cause (Redis) rather than the symptomatic service (order-service)?
- Does Alertmanager inhibition prevent alert storm (5 sources of alerts, should collapse to 1–2)?

**The orchestrator** pre-checks steady state before starting (aborts if error budget < 30%), applies each phase with correct timing, then verifies recovery automatically and posts a pass/fail result to Slack.

---

## Graduation Criteria

An experiment **passes** when ALL of these are true:

| Criterion | How to verify |
|---|---|
| Correct alert fired within 2 minutes | Check Alertmanager firing time vs chaos start time |
| Auto-remediation triggered | Check Argo Workflows for a successful run |
| Recovery to steady state within SLO budget | Error rate back to < 0.01% within budget |
| No data loss | Check order/payment consistency in DB |
| No manual intervention | Confirm no Slack messages asking for help |

---

## Files

```
phase7/
├── kubernetes/
│   └── chaos-mesh/
│       ├── experiments/
│       │   ├── 01-pod-kill.yaml           # + PodDisruptionBudgets
│       │   ├── 02-network-partition.yaml  # 3 variants: latency, partition, loss
│       │   ├── 03-memory-stress.yaml      # + HPA configs
│       │   ├── 04-node-failure.yaml       # + verification queries
│       │   └── 05-cascade-failure.yaml    # Argo orchestrator + 4 chaos phases
│       └── schedules/
│           └── weekly-chaos-schedule.yaml # Automated game days
├── docs/
│   ├── chaos-engineering-philosophy.md    # Why we do this
│   └── gameday-runbook-template.md        # Fill in for every experiment session
├── dashboards/
│   └── chaos-engineering.json            # Live blast radius + recovery metrics
└── scripts/
    └── deploy-phase7.sh
```

---

## Game Day Process

```bash
# 1. Pre-chaos: check steady state
kubectl get pods -n app
# All pods Running + Ready

# Check error budget (must be > 30% before starting)
kubectl port-forward -n observability svc/prometheus 9090:9090
# Query: slo:order_service_availability:error_budget_remaining

# 2. Write your hypothesis (required before applying chaos)
# Copy docs/gameday-runbook-template.md and fill it in

# 3. Apply the experiment
kubectl apply -f kubernetes/chaos-mesh/experiments/01-pod-kill.yaml

# 4. Watch in real time
kubectl port-forward -n observability svc/grafana 3000:80
# Open http://localhost:3000/d/chaos-engineering

# 5. Abort immediately if needed
kubectl delete -f kubernetes/chaos-mesh/experiments/01-pod-kill.yaml

# 6. Post-chaos: verify clean state
kubectl get pods -n app
```

---

## Weekly Automated Schedule

After all 5 experiments pass manually, enable the weekly schedule:

```bash
kubectl apply -f kubernetes/chaos-mesh/schedules/weekly-chaos-schedule.yaml

# Runs:
# Tuesday  10:00 UTC — Pod Kill
# Wednesday 10:00 UTC — Network Latency
# First Monday/month — Cascade Failure (requires manual approval in Argo UI)
```

---

## Design Decisions

### Why write the hypothesis before running the experiment?
The gap between your hypothesis and reality *is the learning*. If you don't write the hypothesis first, you unconsciously adjust your expectations to match what you see. Writing it first forces you to think about failure modes you might not have considered — and the gaps you discover become action items.

### Why escalating blast radius?
Starting with the cascade failure would be like testing a car by driving it off a cliff. Start with pod kill (known, bounded), build confidence, then escalate. If Experiment 1 fails, you have a fundamental problem to fix before worrying about cascades.

### Why scheduled weekly chaos?
A system that passes chaos tests on the day you first test it isn't necessarily reliable. Systems degrade over time — a new deployment changes behaviour, a library update introduces a regression. Weekly automation ensures the system *continuously* proves it can handle failures, not just once.

---

## What's Next

[Phase 8 →](../phase8/README.md) — Track every dollar the platform spends and identify waste automatically
