# Autonomous Observability & Self-Healing Platform

> **The system detects, diagnoses, and heals itself. Humans are involved only for major decisions.**

![Platform Overview](./docs/images/platform-overview.svg)

---

## What This Is

A production-grade, FAANG-level observability platform built on AWS EKS that goes far beyond dashboards. It uses machine learning to detect anomalies, causal inference to find root causes, and reinforcement learning to choose and execute the right remediation — all without waking anyone up.

**Traditional approach:**
```
System breaks → human paged → human diagnoses → human fixes → human monitors recovery
```

**This platform:**
```
System breaks → ML detects (< 60s) → causal engine diagnoses (< 90s) → RL agent heals → human notified
```

---

## Architecture at a Glance

The platform is built across 10 phases, each production-deployable independently:

| Phase | Name | What It Does |
|---|---|---|
| [0](./phase0/README.md) | Foundation | Terraform IaC, EKS, VPC, IAM, KMS, storage |
| [1](./phase1/README.md) | Application Layer | 4 FastAPI microservices, full OTEL instrumentation |
| [2](./phase2/README.md) | SLO Framework | Error budgets, multi-window burn rate alerts |
| [3](./phase3/README.md) | Alerting & Incidents | Alertmanager, OnCall, 6 runbooks, RED dashboard |
| [4](./phase4/README.md) | Self-Healing | Argo Events + Workflows, auto-remediation |
| [5](./phase5/README.md) | ML Intelligence | Prophet, ensemble anomaly detection, causal AI, RL agent |
| [6](./phase6/README.md) | Zero Trust Security | Istio mTLS, Vault, Falco eBPF, OPA, Sigstore |
| [7](./phase7/README.md) | Chaos Engineering | Chaos Mesh, 5 experiments, weekly game days |
| [8](./phase8/README.md) | Cost Observability | OpenCost, rightsizing engine, budget alerts |
| [9–10](./phase9-10/README.md) | FAANG Scale | 3 active regions, 9 cells, Global Accelerator |

---

## Three Pillars

```
Metrics (Prometheus) ─── the WHAT
Logs    (Loki)       ─── the WHY
Traces  (Tempo)      ─── the WHERE

All correlated in Grafana. All feeding the ML layer.
```

---

## Key Numbers

| Metric | Value |
|---|---|
| Anomaly detection time | < 60 seconds |
| Root cause identification | < 90 seconds |
| Manual interventions (steady state) | 0 |
| Blast radius (cell failure) | ~11% of global traffic |
| Regions (active-active) | 3 |
| Cells per region | 3 (9 total) |
| Automation rate target | > 80% of incidents |
| SLO tier 1 target | 99.95% (payment), 99.9% (orders) |

---

## Repository Structure

```
.
├── phase0/          # Foundation: Terraform IaC, EKS, VPC
├── phase1/          # Application: 4 FastAPI services + shared observability
├── phase2/          # SLO: error budgets, burn rate alerts, executive dashboard
├── phase3/          # Alerting: Alertmanager, OnCall, runbooks, RED dashboard
├── phase4/          # Self-healing: Argo Events/Workflows, remediation templates
├── phase5/          # ML: Prophet, anomaly detection, causal AI, RL agent
├── phase6/          # Security: Zero trust, Vault, Falco, OPA, supply chain
├── phase7/          # Chaos: Chaos Mesh, 5 experiments, game day playbook
├── phase8/          # Cost: OpenCost, rightsizing, cost-aware scaling
├── phase9-10/       # Scale: multi-region, cell architecture, capacity planning
└── docs/
    └── images/      # Architecture diagrams (SVG) for all phases
```

---

## Tech Stack

### Infrastructure
| Tool | Purpose |
|---|---|
| AWS EKS (Kubernetes 1.29) | Container orchestration |
| Terraform | Infrastructure as code |
| AWS Global Accelerator | Anycast global routing |
| DynamoDB Global Tables | Cross-region shared state |
| Aurora Global DB | Multi-region PostgreSQL |
| MSK (Kafka) | Event streaming |
| ElastiCache (Redis) | Caching + rate limiting |

### Observability
| Tool | Purpose |
|---|---|
| Prometheus | Metrics collection + alerting |
| Grafana | Dashboards + unified view |
| Loki | Log aggregation (S3-backed) |
| Tempo | Distributed tracing (S3-backed) |
| OpenTelemetry Collector | Unified telemetry pipeline |
| Alertmanager | Alert routing + deduplication |
| Grafana OnCall | On-call scheduling + escalation |

### ML & Intelligence
| Tool | Purpose |
|---|---|
| Prophet | Traffic forecasting (hourly retrain) |
| scikit-learn (Isolation Forest) | Point anomaly detection |
| TensorFlow/Keras (LSTM Autoencoder) | Temporal anomaly detection |
| CUSUM (statsmodels) | Slow drift detection |
| statsmodels (Granger) | Causal inference / RCA |
| Custom Thompson Sampling | RL remediation agent |

### Automation
| Tool | Purpose |
|---|---|
| Argo Events | Event-driven trigger system |
| Argo Workflows | DAG-based remediation workflows |
| KEDA | Event-driven + cost-aware autoscaling |
| Chaos Mesh | Chaos engineering experiments |

### Security
| Tool | Purpose |
|---|---|
| Istio | Service mesh, mTLS, circuit breaking |
| HashiCorp Vault | Secrets management, dynamic credentials |
| Falco | eBPF runtime security monitoring |
| OPA Gatekeeper | Admission control policies |
| Sigstore/Cosign | Supply chain security |
| Grype + Syft | CVE scanning + SBOM generation |

### Cost
| Tool | Purpose |
|---|---|
| OpenCost | Real-time Kubernetes cost allocation |
| Custom rightsizing engine | ML-powered resource recommendations |
| AWS Spot instances | 60–70% compute savings |

---

## Prerequisites

```bash
# Required tools
terraform >= 1.6
kubectl >= 1.29
helm >= 3.12
aws-cli >= 2.0
argocd-cli

# AWS permissions needed
EKS full access
VPC/networking
IAM role creation
KMS key management
DynamoDB
MSK / ElastiCache
RDS / Aurora
S3
Route53
Global Accelerator
```

---

## Quick Start

> **Deploy phases in order.** Each phase builds on the previous.

```bash
# Phase 0: Foundation (start here)
cd phase0
terraform init
terraform plan
terraform apply
./Makefile deploy

# Phase 1: Application Layer
cd ../phase1
kubectl apply -f kubernetes/app/deployments.yaml

# Phase 2–8: Each has a deploy script
cd ../phase2 && bash deploy-observability.sh
cd ../phase4 && bash scripts/deploy-phase4.sh
cd ../phase5 && bash scripts/deploy-phase5.sh
cd ../phase6 && bash scripts/deploy-phase6.sh
cd ../phase7 && bash scripts/deploy-phase7.sh
cd ../phase8 && bash scripts/deploy-phase8.sh

# Phase 9-10: Multi-region (deploy last)
cd ../phase9-10 && bash scripts/deploy-phase9-10.sh
```

---

## Dashboards

After deployment, all dashboards are available in Grafana:

```bash
kubectl port-forward -n observability svc/kube-prometheus-stack-grafana 3000:80
```

| Dashboard | URL | Purpose |
|---|---|---|
| 🌍 Global Command Center | `/d/global-command-center` | Multi-region health, cells, SLO, toil, cost |
| 📊 SLO Executive Overview | `/d/slo-executive-overview` | Error budgets, burn rates |
| 🔴 Service Health (RED) | `/d/service-health-red` | Rate, errors, duration per service |
| 🧠 ML Intelligence | `/d/ml-intelligence` | Forecasts, anomaly scores, RL policy |
| 🔒 Security & Compliance | `/d/security-compliance` | SOC 2 posture, Vault, Falco, OPA |
| 💥 Chaos Engineering | `/d/chaos-engineering` | Live blast radius, recovery metrics |
| 💰 Cost Observability | `/d/cost-observability` | Spend, efficiency, rightsizing |

---

## What Runs Autonomously

| Event | Autonomous Response | Human Involvement |
|---|---|---|
| Pod crash loop | Cordon node → drain → reschedule | Notified via Slack |
| Deployment error spike | Capture state → rollout undo → verify | Notified via Slack |
| Redis memory pressure | Switch eviction policy → predictive scale | Notified via Slack |
| ML anomaly detected | Causal engine → RL agent → Argo Workflow | Notified if critical |
| Traffic spike forecast | Pre-emptive HPA scale-out 30 min ahead | None |
| Security event (Falco) | Falcosidekick → Slack + Alertmanager + Argo | Paged for critical |
| Cost spike | Alert fired + rightsizing recommendation | Weekly digest |
| SLO burn rate critical | Page on-call with runbook | Human resolves |

---

## Architecture Decision Records

Key design decisions are documented in [`phase9-10/docs/architecture-decision-records.md`](./phase9-10/docs/architecture-decision-records.md):

- **ADR-001** — Active-active vs active-passive multi-region
- **ADR-002** — Cell architecture for blast radius control
- **ADR-003** — Thompson Sampling vs Q-learning for RL remediation
- **ADR-004** — Granger causality for root cause analysis
- **ADR-005** — Falco eBPF vs kernel module
- **ADR-006** — Prophet vs pure LSTM for forecasting
- **ADR-007** — OPA Gatekeeper vs Kyverno

---

## Contributing

1. Each phase is independently deployable — changes to one phase don't require redeploying others
2. All ML models expose `/metrics` endpoints — add Prometheus scraping for any new model
3. New runbooks go in `phase3/docs/runbooks/` — follow the existing decision-tree format
4. New chaos experiments go in `phase7/kubernetes/chaos-mesh/experiments/` — write a hypothesis first

---

## License

MIT
