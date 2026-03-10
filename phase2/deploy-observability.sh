#!/bin/bash
# deploy-observability.sh
# Deploy the full observability stack to EKS
# Run after: make kubeconfig (from Phase 0)

set -euo pipefail

NAMESPACE="observability"
CLUSTER_NAME="${CLUSTER_NAME:-autonomous-observability}"
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "🚀 Deploying observability stack to cluster: $CLUSTER_NAME"

# ── Add Helm repos ────────────────────────────────────────────────────────────
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

# ── Create S3 buckets for Loki + Tempo ───────────────────────────────────────
echo "📦 Creating S3 buckets..."
for bucket in \
  "autonomous-observability-loki-chunks" \
  "autonomous-observability-loki-ruler" \
  "autonomous-observability-loki-admin" \
  "autonomous-observability-tempo-traces"; do
  aws s3api create-bucket \
    --bucket "$bucket" \
    --region "$AWS_REGION" \
    --create-bucket-configuration LocationConstraint="$AWS_REGION" 2>/dev/null || true
  aws s3api put-bucket-versioning \
    --bucket "$bucket" \
    --versioning-configuration Status=Enabled
  echo "  ✅ $bucket"
done

# ── Create secrets ────────────────────────────────────────────────────────────
echo "🔑 Creating secrets..."

# Grafana admin credentials
kubectl create secret generic grafana-admin-credentials \
  --namespace "$NAMESPACE" \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$(openssl rand -base64 24)" \
  --dry-run=client -o yaml | kubectl apply -f -

# Slack webhook (replace with your actual webhook)
kubectl create secret generic slack-webhook-secret \
  --namespace "$NAMESPACE" \
  --from-literal=webhook-url="${SLACK_WEBHOOK_URL:-https://hooks.slack.com/services/REPLACE_ME}" \
  --dry-run=client -o yaml | kubectl apply -f -

# PagerDuty routing key (replace with your actual key)
kubectl create secret generic pagerduty-secret \
  --namespace "$NAMESPACE" \
  --from-literal=routing-key="${PAGERDUTY_ROUTING_KEY:-REPLACE_ME}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ── Deploy kube-prometheus-stack ──────────────────────────────────────────────
echo "📊 Deploying Prometheus + Grafana + Alertmanager..."
helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace "$NAMESPACE" \
  --values kubernetes/observability/helm-values.yaml \
  --wait \
  --timeout 10m

# ── Deploy Loki ───────────────────────────────────────────────────────────────
echo "📋 Deploying Loki..."
helm upgrade --install loki \
  grafana/loki \
  --namespace "$NAMESPACE" \
  --set loki.storage.type=s3 \
  --set loki.storage.s3.region="$AWS_REGION" \
  --wait \
  --timeout 5m

# ── Deploy Tempo ──────────────────────────────────────────────────────────────
echo "🔗 Deploying Tempo..."
helm upgrade --install tempo \
  grafana/tempo-distributed \
  --namespace "$NAMESPACE" \
  --set storage.trace.backend=s3 \
  --set "storage.trace.s3.bucket=autonomous-observability-tempo-traces" \
  --set "storage.trace.s3.region=$AWS_REGION" \
  --wait \
  --timeout 5m

# ── Apply recording rules + alert rules ──────────────────────────────────────
echo "🔔 Applying SLO recording rules and alert rules..."
kubectl apply -f alerts/slo-recording-rules.yaml
kubectl apply -f alerts/slo-burn-rate-alerts.yaml

# ── Apply Alertmanager config ─────────────────────────────────────────────────
echo "📬 Applying Alertmanager routing config..."
kubectl apply -f kubernetes/observability/alertmanager-config.yaml

# ── Import Grafana dashboards ─────────────────────────────────────────────────
echo "📈 Waiting for Grafana to be ready..."
kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/name=grafana \
  -n "$NAMESPACE" \
  --timeout=120s

GRAFANA_POD=$(kubectl get pod -n "$NAMESPACE" -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].metadata.name}')
GRAFANA_PASSWORD=$(kubectl get secret grafana-admin-credentials -n "$NAMESPACE" -o jsonpath='{.data.admin-password}' | base64 -d)

echo "📊 Importing dashboards..."
for dashboard in dashboards/*.json; do
  kubectl exec -n "$NAMESPACE" "$GRAFANA_POD" -- \
    curl -s -X POST \
    -H "Content-Type: application/json" \
    -u "admin:$GRAFANA_PASSWORD" \
    -d "{\"dashboard\": $(cat $dashboard), \"overwrite\": true}" \
    http://localhost:3000/api/dashboards/db
  echo "  ✅ Imported $dashboard"
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "✅ Phase 2 deployment complete!"
echo ""
echo "Access Grafana:"
echo "  kubectl port-forward -n $NAMESPACE svc/kube-prometheus-stack-grafana 3000:80"
echo "  Then open: http://localhost:3000"
echo "  Username: admin"
echo "  Password: $GRAFANA_PASSWORD"
echo ""
echo "Verify alerts are loaded:"
echo "  kubectl port-forward -n $NAMESPACE svc/kube-prometheus-stack-prometheus 9090:9090"
echo "  Then open: http://localhost:9090/alerts"
