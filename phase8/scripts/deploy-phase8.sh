#!/bin/bash
# scripts/deploy-phase8.sh
set -euo pipefail

echo "💰 Deploying Phase 8 — Cost Observability"

# ── Install OpenCost ──────────────────────────────────────────────────────────
echo "📊 Installing OpenCost..."
helm repo add opencost https://opencost.github.io/opencost-helm-chart
helm repo update

kubectl create namespace cost --dry-run=client -o yaml | kubectl apply -f -

# Apply IRSA service account first
kubectl apply -f kubernetes/opencost/opencost-config.yaml

helm upgrade --install opencost opencost/opencost \
  --namespace cost \
  --set opencost.exporter.aws.region=us-east-1 \
  --set opencost.prometheus.internal.enabled=true \
  --set opencost.prometheus.internal.serviceName=kube-prometheus-stack-prometheus \
  --set opencost.prometheus.internal.namespaceName=observability \
  --set opencost.prometheus.internal.port=9090 \
  --set metrics.serviceMonitor.enabled=true \
  --set metrics.serviceMonitor.additionalLabels.prometheus=kube-prometheus \
  --wait \
  --timeout 5m

echo "✅ OpenCost installed"

# ── Install KEDA ──────────────────────────────────────────────────────────────
echo "📈 Installing KEDA..."
helm repo add kedacore https://kedacore.github.io/charts
helm repo update

helm upgrade --install keda kedacore/keda \
  --namespace keda \
  --create-namespace \
  --wait \
  --timeout 5m

echo "✅ KEDA installed"

# ── Apply cost recording rules ────────────────────────────────────────────────
echo "📐 Applying cost recording rules..."
kubectl apply -f kubernetes/opencost/opencost-config.yaml

# ── Apply cost alert rules ────────────────────────────────────────────────────
echo "🔔 Applying cost alert rules..."
kubectl apply -f alerts/cost-alert-rules.yaml

# ── Apply KEDA cost-aware ScaledObjects ───────────────────────────────────────
echo "📈 Applying cost-aware KEDA ScaledObjects..."
kubectl apply -f kubernetes/keda/cost-aware-scaling.yaml

# ── Deploy rightsizing engine ─────────────────────────────────────────────────
echo "🔍 Deploying rightsizing engine..."
kubectl apply -f kubernetes/rightsizing/rightsizing-deployment.yaml

# Wait for rightsizing engine
kubectl wait --for=condition=available deployment/rightsizing-engine \
  -n ml --timeout=120s
echo "✅ Rightsizing engine running"

# ── Import Cost dashboard ─────────────────────────────────────────────────────
echo "📊 Importing Cost Observability dashboard..."
GRAFANA_PASSWORD=$(kubectl get secret grafana-admin-credentials -n observability \
  -o jsonpath='{.data.admin-password}' | base64 -d)
GRAFANA_POD=$(kubectl get pod -n observability -l app.kubernetes.io/name=grafana \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n observability "$GRAFANA_POD" -- \
  curl -s -X POST \
  -H "Content-Type: application/json" \
  -u "admin:$GRAFANA_PASSWORD" \
  -d "{\"dashboard\": $(cat dashboards/cost-observability.json), \"overwrite\": true}" \
  http://localhost:3000/api/dashboards/db

echo ""
echo "✅ Phase 8 deployed — Cost Observability active!"
echo ""
echo "════════════════════════════════════════════"
echo "  COST OBSERVABILITY SUMMARY"
echo "════════════════════════════════════════════"
echo ""
echo "Dashboard:"
echo "  kubectl port-forward -n observability svc/kube-prometheus-stack-grafana 3000:80"
echo "  Open: http://localhost:3000/d/cost-observability"
echo ""
echo "OpenCost UI:"
echo "  kubectl port-forward -n cost svc/opencost-ui 9090:9090"
echo "  Open: http://localhost:9090"
echo ""
echo "Key metrics now available:"
echo "  cost:cluster_total_per_hour:dollars      — real-time spend"
echo "  cost:order_service_per_1k_requests:dollars  — unit economics"
echo "  cost:cpu_efficiency_ratio                — waste signal"
echo "  rightsizing_estimated_monthly_savings_dollars — savings opportunity"
echo ""
echo "Rightsizing recommendations (available after first weekly run):"
echo "  kubectl logs -n ml deployment/rightsizing-engine --tail=50"
echo ""
echo "Apply rightsizing patches (AFTER human review):"
echo "  kubectl exec -n ml deployment/rightsizing-engine -- cat /tmp/rightsizing-patch.sh"
echo "  # Review the output, then:"
echo "  kubectl exec -n ml deployment/rightsizing-engine -- bash /tmp/rightsizing-patch.sh"
echo ""
echo "What to act on first:"
echo "  1. Check 'Potential Monthly Savings' stat in dashboard"
echo "  2. Look at 'CPU Efficiency per Deployment' — anything < 40% is waste"
echo "  3. Check 'Cost per 1k requests' — rising = investigate"
echo "  4. Run: kubectl top pods -n app --sort-by=cpu"
