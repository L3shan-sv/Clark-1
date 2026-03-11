#!/bin/bash
# scripts/deploy-phase5.sh
set -euo pipefail

echo "🧠 Deploying Phase 5 — ML Intelligence Layer"

REGISTRY="${REGISTRY:-your-registry}"
TAG="${TAG:-latest}"

# ── Build and push ML service images ─────────────────────────────────────────
echo "🐳 Building ML service images..."

for svc in traffic-forecasting anomaly-detection causal-inference rl-agent drift-detection; do
  echo "  Building $svc..."
  docker build \
    -t "$REGISTRY/$svc:$TAG" \
    -f "ml/$svc/Dockerfile" \
    "ml/$svc/"
  docker push "$REGISTRY/$svc:$TAG"
  echo "  ✅ $svc pushed"
done

# ── Create ml namespace ───────────────────────────────────────────────────────
kubectl create namespace ml --dry-run=client -o yaml | kubectl apply -f -

# ── Create Redis credentials secret ──────────────────────────────────────────
kubectl create secret generic redis-credentials \
  --namespace ml \
  --from-literal=url="${REDIS_URL:-redis://redis.app.svc.cluster.local:6379}" \
  --dry-run=client -o yaml | kubectl apply -f -

# ── Copy Slack secret to ml namespace ────────────────────────────────────────
kubectl get secret slack-webhook-secret -n observability -o json | \
  jq 'del(.metadata.namespace,.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp)' | \
  kubectl apply -n ml -f -

# ── Deploy ML services ────────────────────────────────────────────────────────
echo "🚀 Deploying ML services..."
kubectl apply -f kubernetes/ml/ml-deployments.yaml
kubectl apply -f kubernetes/ml/ml-alert-rules.yaml

# ── Wait for deployments ──────────────────────────────────────────────────────
echo "⏳ Waiting for ML services to start (this takes a while — model training)..."
for dep in traffic-forecaster anomaly-detector causal-engine rl-agent drift-detector; do
  kubectl wait --for=condition=available deployment/$dep \
    -n ml --timeout=300s
  echo "  ✅ $dep ready"
done

# ── Import dashboard ──────────────────────────────────────────────────────────
echo "📊 Importing ML Intelligence dashboard..."
GRAFANA_PASSWORD=$(kubectl get secret grafana-admin-credentials -n observability \
  -o jsonpath='{.data.admin-password}' | base64 -d)
GRAFANA_POD=$(kubectl get pod -n observability -l app.kubernetes.io/name=grafana \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n observability "$GRAFANA_POD" -- \
  curl -s -X POST \
  -H "Content-Type: application/json" \
  -u "admin:$GRAFANA_PASSWORD" \
  -d "{\"dashboard\": $(cat dashboards/ml-intelligence.json), \"overwrite\": true}" \
  http://localhost:3000/api/dashboards/db

echo ""
echo "✅ Phase 5 deployed!"
echo ""
echo "ML Service URLs (via port-forward):"
echo "  kubectl port-forward -n ml svc/rl-agent 8080:8000"
echo "  curl http://localhost:8080/policy   # RL agent policy report"
echo ""
echo "📊 ML Intelligence Dashboard:"
echo "  kubectl port-forward -n observability svc/kube-prometheus-stack-grafana 3000:80"
echo "  Open: http://localhost:3000/d/ml-intelligence"
echo ""
echo "🌡️  Check drift status:"
echo "  kubectl logs -n ml deployment/drift-detector --tail=50"
echo ""
echo "⚠️  RL Agent is in SHADOW MODE"
echo "   Monitor for 50+ incidents then set SHADOW_MODE=false to enable auto-execution"
