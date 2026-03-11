# docs/architecture-decision-records.md
# Architecture Decision Records (ADRs)
# This document captures the key design decisions and the reasoning behind them.
# Every non-obvious choice should have an ADR. Future engineers will thank you.

---

## ADR-001: Active-Active Multi-Region vs Active-Passive

**Status:** Accepted
**Date:** 2024-01

**Context:**
We needed to decide how to handle regional failures. The two main options were:
active-passive (one live region, one standby) and active-active (all regions serve traffic).

**Decision:**
Active-active with AWS Global Accelerator.

**Reasoning:**
- Active-passive wastes 50% of provisioned capacity sitting idle
- Failover in active-passive takes 2-5 minutes (DNS TTL, cold start)
- Active-active gives sub-second failover (Global Accelerator uses anycast)
- All capacity is utilised — no "warm standby" cost
- Latency is lower because users hit their nearest region

**Trade-offs:**
- Requires distributed state management (DynamoDB Global Tables)
- More complex testing (need to test cross-region traffic shifting)
- Higher operational complexity — addressed by this platform

---

## ADR-002: Cell Architecture for Blast Radius Control

**Status:** Accepted
**Date:** 2024-01

**Context:**
A single deployment failure in a monolithic cluster affects all customers.
We needed a way to limit blast radius.

**Decision:**
Cell-based architecture: 3 cells per region, customers hashed to cells.

**Reasoning:**
- A bad deploy to one cell affects 1/3 of a region's customers, not all
- Canary deploys become natural: deploy to cell-0, verify, roll to cell-1, cell-2
- Noisy neighbour isolation: one customer's spike can't swamp another cell
- Cell evacuation is clean: update routing, drain cell, fix, re-enable

**Trade-offs:**
- 3× the number of Deployments, Services, ConfigMaps
- Routing complexity (solved by Envoy/Lua filter)
- Resource overhead: minimum 2 pods per service per cell = 6 pods minimum
- Mitigated by: ResourceQuotas per cell prevent overconsumption

**Alternatives considered:**
- Namespace-per-customer: doesn't scale beyond ~100 customers
- Single namespace with HPA: no isolation guarantee

---

## ADR-003: Thompson Sampling for RL Remediation vs Q-Learning

**Status:** Accepted
**Date:** 2024-01

**Context:**
We needed an RL algorithm for automated incident remediation. The algorithm
must work with sparse rewards (incidents are infrequent) and must be
safe to deploy in shadow mode first.

**Decision:**
Multi-armed bandit with Thompson Sampling.

**Reasoning:**
- Full RL (Q-learning, PPO) requires a simulation environment we don't have
- Incidents are sparse — we might see 5-10 per week. Full RL needs thousands.
- Thompson Sampling works excellently with sparse data (Beta distribution)
- The "exploration vs exploitation" balance is automatic (sample from Beta)
- Per-incident-class bandits are interpretable: you can see exactly which
  action has the highest success rate for each incident type
- Retrains on every incident — no weekly batch job needed

**Trade-offs:**
- Cannot learn multi-step remediation sequences (just single action selection)
- Stationary assumption: assumes success rate of actions doesn't change
  (addressed by drift detection on the policy)
- No contextual features (future work: contextual bandit)

---

## ADR-004: Granger Causality for Root Cause Analysis

**Status:** Accepted
**Date:** 2024-01

**Context:**
Traditional RCA approaches use correlation. But correlation is symmetric:
"A correlates with B" tells you nothing about causation direction.
We needed to determine whether payment-service degradation CAUSES
order-service errors, or vice versa.

**Decision:**
Granger causality tests on time-series metric pairs from the dependency graph.

**Reasoning:**
- Granger causality tests whether past values of X predict future values of Y
  (above and beyond Y's own history) — this gives us temporal precedence
- Combined with the dependency graph, temporal precedence = causation
- Fast: runs in milliseconds on 30 minutes of 1-minute-resolution data
- Interpretable: p-value tells you how confident the causal claim is
- Open source: statsmodels, no proprietary ML platform needed

**Trade-offs:**
- Granger causality ≠ true causality. It's a statistical test, not a proof.
- Requires sufficient historical data (< 15 data points = unreliable)
- Doesn't handle non-linear relationships well (LSTM would be better but slower)
- Confounding variables can produce false positives
- Mitigated by: combining Granger p-value with Pearson correlation and
  graph distance in the confidence score

---

## ADR-005: Falco eBPF vs Kernel Module for Runtime Security

**Status:** Accepted
**Date:** 2024-01

**Context:**
Falco supports two instrumentation modes: kernel module and eBPF.
Both intercept syscalls in real time.

**Decision:**
eBPF mode.

**Reasoning:**
- Kernel modules are compiled against a specific kernel version — break on upgrades
- eBPF is verified by the kernel before loading — cannot crash the kernel
- EKS Fargate doesn't support kernel modules; eBPF works everywhere
- AWS publishes eBPF-compatible EKS AMIs by default
- eBPF overhead is < 1% at typical Falco scan rates

**Trade-offs:**
- Requires kernel 4.14+ (all EKS 1.25+ nodes meet this requirement)
- CO-RE (Compile Once — Run Everywhere) eBPF programs are larger binary
- Some Falco rules require kernel module for full coverage (rare edge cases)

---

## ADR-006: Prophet for Traffic Forecasting vs LSTM-only

**Status:** Accepted
**Date:** 2024-01

**Context:**
We needed a time-series forecasting model for predictive autoscaling.
Options: Prophet, pure LSTM, ARIMA, or hybrid.

**Decision:**
Prophet for traffic forecasting, LSTM Autoencoder for anomaly detection only.

**Reasoning:**
- Prophet natively models daily + weekly seasonality (traffic spikes at 9am, Mon-Fri)
- Prophet retrains in seconds — LSTM takes minutes
- Prophet provides uncertainty intervals for conservative scaling (scale to upper bound)
- Prophet handles missing data and outliers without preprocessing
- LSTM is better at pattern anomaly detection (its strength is reconstruction error)
- Using the right tool for each job beats a single model that does both poorly

**Trade-offs:**
- Prophet assumes additive/multiplicative seasonality — breaks for truly novel patterns
- Prophet doesn't capture sudden structural changes (new feature launch doubling traffic)
- Mitigated by: drift detection triggers retraining when MAPE > 30%

---

## ADR-007: OPA Gatekeeper vs Kyverno for Policy as Code

**Status:** Accepted
**Date:** 2024-01

**Context:**
We needed admission control to enforce security policies at deploy time.
Main options: OPA Gatekeeper (Rego), Kyverno (YAML-based).

**Decision:**
OPA Gatekeeper.

**Reasoning:**
- Rego is Turing-complete — can express any policy
- OPA has a mature testing framework (opa test)
- Rego policies are unit-testable with mock input documents
- Large ecosystem of pre-written constraints (library.gatekeeper.sh)
- CNCF graduated project — long-term stability
- Kyverno YAML policies become hard to reason about at scale

**Trade-offs:**
- Rego has a learning curve — not immediately readable to newcomers
- OPA requires a separate policy framework installation
- Kyverno would be simpler for simple use cases
- Mitigated by: all policies documented with SOC 2 control references

---

## System Capabilities Summary

After all 10 phases:

| Capability | Implementation | FAANG-Level? |
|---|---|---|
| Metrics | Prometheus HA | ✅ |
| Logs | Loki S3-backed | ✅ |
| Traces | Tempo S3-backed | ✅ |
| SLO Framework | Multi-window burn rate | ✅ |
| Auto-remediation | Argo Workflows | ✅ |
| Traffic Forecasting | Prophet + KEDA | ✅ |
| Anomaly Detection | Ensemble (IF+LSTM+CUSUM) | ✅ |
| Root Cause Analysis | Granger causality + graph BFS | ✅ |
| RL Remediation | Thompson Sampling bandit | ✅ |
| Model Drift Detection | PSI + KS + ADWIN | ✅ |
| Zero Trust Networking | Istio mTLS STRICT | ✅ |
| Dynamic Secrets | HashiCorp Vault | ✅ |
| Runtime Security | Falco eBPF | ✅ |
| Policy as Code | OPA Gatekeeper | ✅ |
| Supply Chain Security | Cosign + Grype + Syft | ✅ |
| Chaos Engineering | Chaos Mesh + GameDays | ✅ |
| Cost Observability | OpenCost + rightsizing | ✅ |
| Multi-region Active-Active | 3 regions + Global Accelerator | ✅ |
| Cell Architecture | 9 cells (3 regions × 3 cells) | ✅ |
| Adaptive Traffic Shaping | Load shedding + circuit breaking | ✅ |
| Capacity Planning | Prophet + linear forecast | ✅ |
| Toil Tracking | Automated weekly report | ✅ |
