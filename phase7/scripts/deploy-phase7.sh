#!/bin/bash
# scripts/deploy-phase7.sh
set -euo pipefail

echo "💥 Deploying Phase 7 — Chaos Engineering"

# ── Install Chaos Mesh ────────────────────────────────────────────────────────
echo "🌀 Installing Chaos Mesh..."
helm repo add chaos-mesh https://charts.chaos-mesh.org
helm repo update

kubectl create namespace chaos --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh \
  --namespace chaos \
  --set controllerManager.replicaCount=3 \
  --set chaosDaemon.runtime=containerd \
  --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
  --wait \
  --timeout 5m

echo "✅ Chaos Mesh installed"

# ── RBAC for chaos workflows ──────────────────────────────────────────────────
kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: chaos-operator
  namespace: chaos
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: chaos-operator
rules:
  - apiGroups: ["chaos-mesh.org"]
    resources: ["*"]
    verbs: ["*"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: chaos-operator
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: chaos-operator
subjects:
  - kind: ServiceAccount
    name: chaos-operator
    namespace: chaos
EOF

# ── Apply PodDisruptionBudgets ────────────────────────────────────────────────
echo "🛡️  Applying PodDisruptionBudgets..."
kubectl apply -f kubernetes/chaos-mesh/experiments/01-pod-kill.yaml \
  --dry-run=client -o yaml | grep -A 20 "kind: PodDisruptionBudget" | kubectl apply -f -

# ── Apply HPA configs ─────────────────────────────────────────────────────────
echo "📈 Applying HPA configurations..."
kubectl apply -f kubernetes/chaos-mesh/experiments/03-memory-stress.yaml

# ── Import Chaos dashboard ────────────────────────────────────────────────────
echo "📊 Importing Chaos Engineering dashboard..."
GRAFANA_PASSWORD=$(kubectl get secret grafana-admin-credentials -n observability \
  -o jsonpath='{.data.admin-password}' | base64 -d)
GRAFANA_POD=$(kubectl get pod -n observability -l app.kubernetes.io/name=grafana \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n observability "$GRAFANA_POD" -- \
  curl -s -X POST \
  -H "Content-Type: application/json" \
  -u "admin:$GRAFANA_PASSWORD" \
  -d "{\"dashboard\": $(cat dashboards/chaos-engineering.json), \"overwrite\": true}" \
  http://localhost:3000/api/dashboards/db

echo ""
echo "✅ Phase 7 deployed!"
echo ""
echo "════════════════════════════════════════════"
echo "  CHAOS ENGINEERING READY"
echo "════════════════════════════════════════════"
echo ""
echo "Chaos Mesh dashboard:"
echo "  kubectl port-forward -n chaos svc/chaos-dashboard 2333:2333"
echo "  Open: http://localhost:2333"
echo ""
echo "Run experiments (in order):"
echo ""
echo "  Experiment 1 — Pod Kill (safest, start here):"
echo "    kubectl apply -f kubernetes/chaos-mesh/experiments/01-pod-kill.yaml"
echo "    # Watch: kubectl get pods -n app -w"
echo ""
echo "  Experiment 2 — Network Partition:"
echo "    kubectl apply -f kubernetes/chaos-mesh/experiments/02-network-partition.yaml"
echo "    # Watch error rate in Grafana: http://grafana/d/chaos-engineering"
echo ""
echo "  Experiment 3 — Memory Stress:"
echo "    kubectl apply -f kubernetes/chaos-mesh/experiments/03-memory-stress.yaml"
echo ""
echo "  Experiment 4 — Node Failure:"
echo "    kubectl apply -f kubernetes/chaos-mesh/experiments/04-node-failure.yaml"
echo ""
echo "  Experiment 5 — CASCADE FAILURE (run last, requires approval):"
echo "    kubectl create -f kubernetes/chaos-mesh/experiments/05-cascade-failure.yaml"
echo "    # Approve in Argo UI: http://localhost:2746"
echo ""
echo "  Abort any experiment immediately:"
echo "    kubectl delete -f kubernetes/chaos-mesh/experiments/0X-experiment.yaml"
echo ""
echo "Enable weekly schedule (only after all 5 pass):"
echo "    kubectl apply -f kubernetes/chaos-mesh/schedules/weekly-chaos-schedule.yaml"
