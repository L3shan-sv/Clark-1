#!/bin/bash
# deploy-phase4.sh
# Install Argo Events + Argo Workflows and apply all remediation templates

set -euo pipefail

echo "🤖 Deploying Phase 4 — Self-Healing Automation"

# ── Install Argo Workflows ────────────────────────────────────────────────────
echo "📦 Installing Argo Workflows..."
kubectl create namespace argo --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -n argo -f \
  https://github.com/argoproj/argo-workflows/releases/download/v3.5.4/install.yaml

# ── Install Argo Events ───────────────────────────────────────────────────────
echo "📦 Installing Argo Events..."
kubectl apply -f \
  https://raw.githubusercontent.com/argoproj/argo-events/v1.9.0/manifests/install.yaml

kubectl apply -n argo -f \
  https://raw.githubusercontent.com/argoproj/argo-events/v1.9.0/manifests/install-validating-webhook.yaml

# ── Apply RBAC and EventBus ───────────────────────────────────────────────────
echo "🔑 Applying RBAC and EventBus..."
kubectl apply -f kubernetes/argo-events/install.yaml

# ── Wait for controllers to be ready ─────────────────────────────────────────
echo "⏳ Waiting for Argo controllers..."
kubectl wait --for=condition=available deployment/workflow-controller \
  -n argo --timeout=120s
kubectl wait --for=condition=available deployment/argo-events-controller-manager \
  -n argo --timeout=120s

# ── Apply EventSource ─────────────────────────────────────────────────────────
echo "📡 Applying EventSource (Alertmanager webhook receiver)..."
kubectl apply -f kubernetes/argo-events/event-sources/alertmanager-webhook.yaml

# ── Apply Sensors ─────────────────────────────────────────────────────────────
echo "🎯 Applying Sensors (event → workflow routing)..."
kubectl apply -f kubernetes/argo-events/sensors/remediation-sensors.yaml

# ── Apply Workflow Templates ──────────────────────────────────────────────────
echo "⚙️  Applying Workflow Templates..."
kubectl apply -f kubernetes/argo-workflows/templates/pod-crashloop-remediation.yaml
kubectl apply -f kubernetes/argo-workflows/templates/deployment-auto-rollback.yaml
kubectl apply -f kubernetes/argo-workflows/templates/redis-and-scale-remediation.yaml

# ── Create audit log ConfigMap ────────────────────────────────────────────────
echo "📝 Creating audit log ConfigMap..."
kubectl create configmap remediation-audit-log \
  --namespace argo \
  --dry-run=client -o yaml | kubectl apply -f -

# ── Verify ────────────────────────────────────────────────────────────────────
echo ""
echo "✅ Phase 4 deployed! Verifying..."
echo ""
echo "EventSources:"
kubectl get eventsource -n argo
echo ""
echo "Sensors:"
kubectl get sensor -n argo
echo ""
echo "WorkflowTemplates:"
kubectl get workflowtemplate -n argo
echo ""
echo "🔗 Argo Workflows UI:"
echo "   kubectl port-forward -n argo svc/argo-server 2746:2746"
echo "   Open: http://localhost:2746"
echo ""
echo "📋 View audit log:"
echo "   kubectl get configmap remediation-audit-log -n argo -o yaml"
echo ""
echo "🧪 Test a workflow manually:"
echo "   kubectl create -f kubernetes/argo-workflows/workflows/test-rollback.yaml"
