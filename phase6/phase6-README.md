# Phase 6 — Zero Trust Security

![Architecture](../docs/images/phase6-architecture.svg)

## Overview

Phase 6 implements defence in depth across five security layers. The guiding principle is zero trust: **every request is authenticated, every secret is dynamic, every workload is monitored at the syscall level, and every deployment is verified before it runs.**

"Trust but verify" is not zero trust. Zero trust is: **never trust, always verify.**

---

## Layer 1: Network — Istio Service Mesh

### mTLS STRICT mode
Every pod-to-pod connection is mutually authenticated with TLS. There is no plaintext traffic in the `app` or `ml` namespaces.

```yaml
apiVersion: security.istio.io/v1beta1
kind: PeerAuthentication
metadata:
  name: default
  namespace: app
spec:
  mtls:
    mode: STRICT    # Refuse all non-mTLS connections
```

### Default deny, explicit allow
The default `AuthorizationPolicy` denies all traffic. Every permitted flow is explicitly listed:

```
order-service    → payment-service   (POST /payments only)
payment-service  → order-service     (callback only)
Prometheus       → all services      (GET /metrics only)
Ingress gateway  → order-service     (public endpoints)
```

Any traffic not on this list is silently dropped and logged.

### SPIFFE/SPIRE identities
Every pod gets a cryptographic SPIFFE identity: `spiffe://cluster.local/ns/app/sa/order-service`. These identities are verified by Istio during every mTLS handshake — no IP-based identity, no JWT-based identity that can be stolen.

---

## Layer 2: Secrets — HashiCorp Vault HA

### Dynamic credentials (zero static secrets)
Vault generates database passwords on-demand, valid for 1 hour, then rotated automatically. The password that order-service uses right now has never been seen by any human engineer.

```
order-service starts
    ↓
Vault Agent Injector (sidecar) authenticates via K8s ServiceAccount token
    ↓
Vault issues: username=order-service-1234, password=abc...xyz, TTL=1h
    ↓
Credentials written to /vault/secrets/db.env (file, not env var)
    ↓
order-service reads file
    ↓
After 1h: Vault agent automatically renews or rotates
```

Secrets are **never** visible in:
- `kubectl describe pod`
- Environment variables
- Kubernetes Secrets objects
- CI/CD pipeline logs

### AWS KMS auto-unseal
Vault unseals itself using AWS KMS on every restart. No operator intervention required. No Shamir key shards stored anywhere humans can access.

---

## Layer 3: Runtime — Falco eBPF

Falco intercepts every syscall at the kernel level using eBPF. Eight custom rules, tuned to the workload:

| Rule | MITRE Technique | Severity | Action |
|---|---|---|---|
| Shell spawned in container | T1059 | CRITICAL | Immediate PagerDuty |
| Privilege escalation attempt | T1068 | CRITICAL | Immediate PagerDuty |
| Unexpected outbound connection | T1071 | WARNING | Slack alert |
| Sensitive file read (`/etc/shadow`, etc.) | T1003 | CRITICAL | Immediate PagerDuty |
| Container escape via setns | T1611 | CRITICAL | Immediate PagerDuty |
| Crypto mining detected | T1496 | CRITICAL | Immediate PagerDuty |
| Write to binary directory | T1222 | CRITICAL | Immediate PagerDuty |
| Direct K8s Secrets API access | T1552 | WARNING | Slack alert |

The "unexpected outbound connection" rule uses an allowlist: only connections to Kafka, Redis, PostgreSQL, OTLP collector, Vault, and Prometheus are permitted. Anything else fires the alert.

---

## Layer 4: Admission Control — OPA Gatekeeper

Six constraints enforced at deploy time. Non-compliant manifests are **rejected** before they reach the scheduler:

| Policy | What It Enforces | SOC 2 Control |
|---|---|---|
| `K8sNoRoot` | `runAsRoot: false`, UID ≠ 0 | CC6.1 |
| `K8sApprovedRegistries` | Only images from approved registries | CC6.7 |
| `K8sRequiredResourceLimits` | CPU + memory limits mandatory | CC7.1 |
| `K8sNoPrivileged` | No `privileged: true`, no `SYS_ADMIN` | CC8.1 |
| `K8sReadOnlyRootFs` | `readOnlyRootFilesystem: true` | CC9.2 |
| `K8sRequiredLabels` | `app`, `version`, `team` labels required | warn mode |

The first five use `enforcementAction: deny` — the deployment is blocked. The labels policy uses `warn` — it logs but doesn't block, giving teams time to comply.

---

## Layer 5: Supply Chain — Sigstore

Every container image must be cryptographically signed by the GitHub Actions CI pipeline before it can run in the cluster.

```
CI pipeline:
  1. Build image
  2. Syft → generate SBOM (software bill of materials)
  3. Grype → CVE scan (CRITICAL severity = fail build)
  4. Cosign sign → keyless OIDC signature bound to GitHub Actions identity
  5. Attach SBOM to image in registry
  6. Push to ECR

At deploy time:
  Sigstore Policy Controller checks:
  - Image is signed
  - Signature is from the correct GitHub Actions workflow
  - Signature is not expired
  → Unsigned or unverifiable images are BLOCKED by admission webhook
```

---

## Files

```
phase6/
├── kubernetes/
│   ├── istio/
│   │   └── istio-config.yaml             # PeerAuthentication, AuthorizationPolicy, DestinationRules
│   ├── vault/
│   │   ├── vault-config.yaml             # HA Vault (3 replicas, Raft, KMS unseal)
│   │   └── app-service-vault-annotations.yaml  # Vault Agent Injector annotations
│   ├── falco/
│   │   ├── falco-config.yaml             # 8 custom eBPF rules
│   │   └── security-alert-rules.yaml     # PrometheusRules for Falco events
│   ├── opa/
│   │   └── gatekeeper-policies.yaml      # 6 ConstraintTemplates + Constraints
│   └── supply-chain/
│       └── supply-chain-security.yaml    # Sigstore ClusterImagePolicy
├── dashboards/
│   └── security-compliance.json          # Security posture + SOC 2 live checklist
└── scripts/
    └── deploy-phase6.sh
```

---

## Security Compliance Dashboard

`dashboards/security-compliance.json` provides a live SOC 2 compliance checklist:

- Overall security posture score
- Vault status (sealed/unsealed/HA health)
- OPA policy violations (count + which deployments)
- mTLS coverage % (should be 100%)
- Signed images % (should be 100%)
- Falco events timeline
- CVE status for running images

---

## Deploy

```bash
cd phase6
bash scripts/deploy-phase6.sh

# Verify mTLS is enforced
kubectl exec -n app deployment/order-service -- \
  curl -sk http://payment-service.app.svc.cluster.local/health
# Should return connection refused (no plaintext)

# Verify OPA is blocking non-compliant pods
kubectl run bad-pod --image=nginx --privileged=true -n app
# Should return: Error from server: admission webhook denied the request
```

---

## Design Decisions

### Why inject secrets as files, not environment variables?
Environment variables are visible in `kubectl describe pod`, process listings, and many monitoring tools. They're also copied into child processes. Files in `/vault/secrets/` are accessible only by the container's filesystem — and Vault agent automatically rotates them without restarting the container.

### Why eBPF over kernel module for Falco?
Kernel modules can crash the kernel if there's a bug. eBPF programs are verified by the kernel before loading and run in a sandboxed environment — they cannot crash the kernel. eBPF also works on EKS Fargate and doesn't require kernel headers to compile.

### Why keyless signing with Cosign?
Traditional signing uses long-lived private keys that must be stored, rotated, and protected. Keyless signing uses short-lived OIDC tokens from the CI provider's identity — the signature is tied to "this specific GitHub Actions run in this specific repository." No private keys to manage, no keys to leak.

---

## What's Next

[Phase 7 →](../phase7/README.md) — Prove everything works under failure conditions with chaos engineering
