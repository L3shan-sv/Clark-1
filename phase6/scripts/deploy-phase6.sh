#!/bin/bash
# scripts/deploy-phase6.sh
set -euo pipefail

echo "🔒 Deploying Phase 6 — Zero Trust Security Layer"

# ── Install Istio ─────────────────────────────────────────────────────────────
echo "🕸️  Installing Istio service mesh..."
curl -L https://istio.io/downloadIstio | ISTIO_VERSION=1.20.2 sh -
export PATH="$PWD/istio-1.20.2/bin:$PATH"

istioctl install -f kubernetes/istio/istio-config.yaml --skip-confirmation

# Label namespaces for sidecar injection
for ns in app ml observability; do
  kubectl label namespace $ns istio-injection=enabled --overwrite
  echo "  ✅ $ns — sidecar injection enabled"
done

# ── Rolling restart to inject sidecars ────────────────────────────────────────
echo "🔄 Rolling restart to inject Envoy sidecars..."
for deploy in order-service payment-service notification-service analytics-service; do
  kubectl rollout restart deployment/$deploy -n app
done
kubectl rollout status deployment/order-service -n app --timeout=120s

# Apply Istio policies
kubectl apply -f kubernetes/istio/istio-config.yaml

# ── Install HashiCorp Vault ────────────────────────────────────────────────────
echo "🔑 Installing HashiCorp Vault (HA mode)..."
helm repo add hashicorp https://helm.releases.hashicorp.com
helm repo update

helm upgrade --install vault hashicorp/vault \
  --namespace security \
  --create-namespace \
  --wait \
  --timeout 5m

echo "⏳ Waiting for Vault to be ready..."
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=vault \
  -n security --timeout=120s

echo "🔑 Initialising Vault engines and policies..."
kubectl apply -f kubernetes/vault/vault-config.yaml

# ── Install Falco ─────────────────────────────────────────────────────────────
echo "🦅 Installing Falco (eBPF runtime security)..."
helm repo add falcosecurity https://falcosecurity.github.io/charts
helm repo update

helm upgrade --install falco falcosecurity/falco \
  --namespace security \
  --set driver.kind=ebpf \
  --set falcosidekick.enabled=true \
  --set "falcosidekick.config.slack.webhookurl=${SLACK_WEBHOOK_URL}" \
  --wait \
  --timeout 5m

# Apply custom Falco rules
kubectl patch configmap falco-rules -n security \
  --patch-file kubernetes/falco/falco-config.yaml

# Apply security alert rules
kubectl apply -f kubernetes/falco/security-alert-rules.yaml

# ── Install OPA Gatekeeper ────────────────────────────────────────────────────
echo "⚖️  Installing OPA Gatekeeper..."
helm repo add gatekeeper https://open-policy-agent.github.io/gatekeeper/charts
helm upgrade --install gatekeeper gatekeeper/gatekeeper \
  --namespace gatekeeper-system \
  --create-namespace \
  --wait \
  --timeout 5m

echo "⏳ Waiting for Gatekeeper webhooks..."
kubectl wait --for=condition=ready pod \
  -l control-plane=audit-controller \
  -n gatekeeper-system --timeout=60s

echo "📋 Applying OPA policies..."
kubectl apply -f kubernetes/opa/gatekeeper-policies.yaml

# ── Install Sigstore Policy Controller ────────────────────────────────────────
echo "🔏 Installing Sigstore Policy Controller..."
helm repo add sigstore https://sigstore.github.io/helm-charts
helm upgrade --install policy-controller sigstore/policy-controller \
  --namespace cosign-system \
  --create-namespace \
  --wait \
  --timeout 5m

kubectl apply -f kubernetes/supply-chain/supply-chain-security.yaml

# ── Import security dashboard ─────────────────────────────────────────────────
echo "📊 Importing security compliance dashboard..."
GRAFANA_PASSWORD=$(kubectl get secret grafana-admin-credentials -n observability \
  -o jsonpath='{.data.admin-password}' | base64 -d)
GRAFANA_POD=$(kubectl get pod -n observability -l app.kubernetes.io/name=grafana \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n observability "$GRAFANA_POD" -- \
  curl -s -X POST \
  -H "Content-Type: application/json" \
  -u "admin:$GRAFANA_PASSWORD" \
  -d "{\"dashboard\": $(cat dashboards/security-compliance.json), \"overwrite\": true}" \
  http://localhost:3000/api/dashboards/db

echo ""
echo "✅ Phase 6 deployed — Zero Trust active!"
echo ""
echo "Security posture summary:"
echo "  🕸️  Istio mTLS:   STRICT mode in app/ and ml/ namespaces"
echo "  🔑 Vault:        Dynamic secrets, no long-lived credentials"
echo "  🦅 Falco:        eBPF runtime security, 8 custom rules"
echo "  ⚖️  OPA:          6 constraints blocking insecure deployments"
echo "  🔏 Cosign:        Image signing required — unsigned = blocked"
echo ""
echo "Verify mTLS is working:"
echo "  kubectl exec -n app deployment/order-service -c istio-proxy -- \\"
echo "    pilot-agent request GET /certs | jq .certificates[0].identity"
echo ""
echo "Check OPA policy compliance:"
echo "  kubectl get constraints -A"
echo ""
echo "View Falco events:"
echo "  kubectl logs -n security daemonset/falco --tail=50"
