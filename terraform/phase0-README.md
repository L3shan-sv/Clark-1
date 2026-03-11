# Phase 0 — Foundation & Infrastructure

![Architecture](../docs/images/phase0-architecture.svg)

## Overview

Phase 0 lays the entire infrastructure foundation using Terraform. Everything that follows — the application layer, observability stack, ML models, and security controls — runs on top of what this phase provisions.

**Nothing is clicked in the AWS console. Everything is code.**

---

## What Gets Deployed

### VPC — 3-Tier Network
- **3 Availability Zones** — `us-east-1a/b/c`
- **Public subnets** — ALB, NAT Gateways
- **Private subnets** — Application nodes (no direct internet access)
- **Intra subnets** — Database tier (no internet access at all)
- **1 NAT Gateway per AZ** — no single point of failure on egress

### EKS Cluster — Kubernetes 1.29
| Node Group | Instance | Min | Max | Purpose |
|---|---|---|---|---|
| `system` | m5.large | 2 | 4 | kube-system, CNI, CoreDNS |
| `app` | m5.xlarge | 3 | 30 | All application workloads |
| `observability` | r5.xlarge | 2 | 6 | Prometheus, Loki, Tempo (memory-heavy) |
| `ml` | m5.xlarge | 2 | 8 | ML model servers |

### 8 Namespaces Pre-Created
```
app             # All application microservices
observability   # Prometheus, Grafana, Loki, Tempo, Alertmanager
ml              # ML model servers
argo            # Argo Events + Argo Workflows
chaos           # Chaos Mesh experiments
cost            # OpenCost
istio-system    # Service mesh control plane
vault           # HashiCorp Vault
```

### AWS Services Provisioned
| Service | Configuration |
|---|---|
| **KMS** | Separate keys for EKS secrets + S3 + RDS. Auto-rotation enabled. |
| **IAM + IRSA** | OIDC provider attached. Per-service roles, zero static credentials. |
| **S3** | Terraform state (encrypted, versioned), Loki/Tempo object storage |
| **ECR** | Container registry with image scanning enabled |
| **ACM** | TLS certificates for ALB + Ingress |
| **SSM Parameter Store** | Non-secret configuration values |
| **EBS (gp3)** | Default StorageClass — encrypted, higher throughput than gp2 |

---

## Files

```
phase0/
├── terraform/
│   ├── main.tf              # Root module: VPC, EKS, IAM
│   ├── variables.tf         # All configuration variables
│   ├── outputs.tf           # Cluster endpoint, node group ARNs
│   ├── vpc.tf               # VPC, subnets, routing tables, NAT GWs
│   ├── eks.tf               # EKS cluster, node groups, OIDC
│   ├── iam.tf               # IRSA roles for each service
│   ├── storage.tf           # S3 buckets, StorageClass, EBS CSI
│   └── kms.tf               # KMS keys per purpose
└── Makefile                 # deploy / destroy / kubeconfig
```

---

## Deploy

```bash
cd phase0

# 1. Configure AWS credentials
export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1

# 2. Init and deploy
terraform init
terraform plan -out=tfplan
terraform apply tfplan

# 3. Configure kubectl
aws eks update-kubeconfig \
  --region us-east-1 \
  --name autonomous-observability

# 4. Verify
kubectl get nodes
kubectl get namespaces
```

Expected output after apply:
```
Apply complete! Resources: 47 added, 0 changed, 0 destroyed.

Outputs:
cluster_endpoint = "https://XXXX.gr7.us-east-1.eks.amazonaws.com"
cluster_name     = "autonomous-observability"
```

---

## Design Decisions

### Why gp3 over gp2?
gp3 provides 3,000 IOPS and 125 MiB/s baseline throughput at lower cost than gp2. Prometheus writes are IOPS-intensive — this matters.

### Why one NAT Gateway per AZ?
A single NAT Gateway is a cross-AZ single point of failure. During an AZ outage, nodes in other AZs would lose outbound internet access. The extra cost (~$45/month) is worth the resilience.

### Why IRSA over node instance profiles?
Node instance profiles grant the same AWS permissions to every pod on a node. IRSA (IAM Roles for Service Accounts) binds an IAM role to a specific Kubernetes ServiceAccount — so only the Prometheus pod can read CloudWatch, and only the Vault pod can call KMS. Principle of least privilege at the pod level.

---

## Prerequisites

- Terraform >= 1.6
- AWS CLI >= 2.0 with appropriate permissions
- kubectl >= 1.29

## What's Next

[Phase 1 →](../phase1/README.md) — Deploy the 4 application microservices on this foundation
