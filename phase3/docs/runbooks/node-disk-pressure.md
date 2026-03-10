# Runbook: Node Disk Pressure

**Alert:** `NodeDiskPressure`
**Severity:** Warning (> 75%) / Critical (> 90%)
**SLO Impact:** Tier 3 — Infrastructure
**Last Updated:** 2024-01-01

---

## What Is Happening

An EKS node's disk is filling up. Kubernetes will start evicting pods from nodes with disk pressure. This can cascade into pod restarts and availability issues.

---

## Immediate Actions

**1. Identify which node**
```bash
kubectl describe nodes | grep -A5 "DiskPressure"
kubectl get nodes -o custom-columns="NAME:.metadata.name,DISK:.status.conditions[?(@.type=='DiskPressure')].status"
```

**2. Check what's consuming disk**
```bash
NODE_NAME=<node-name>
kubectl debug node/$NODE_NAME -it --image=ubuntu -- df -h
kubectl debug node/$NODE_NAME -it --image=ubuntu -- du -sh /var/log/containers/*
```

**3. Check for large container logs**
```bash
# Containers with excessive logging are the most common cause
kubectl debug node/$NODE_NAME -it --image=ubuntu -- \
  find /var/log/containers -name "*.log" -size +500M
```

**4. Clean up terminated container logs (safe)**
```bash
kubectl debug node/$NODE_NAME -it --image=ubuntu -- \
  find /var/log/pods -name "*.log" -mtime +1 -delete
```

---

## Mitigation

**Cordon the node (stop new pods scheduling on it):**
```bash
kubectl cordon $NODE_NAME
```

**Drain the node (move pods to healthy nodes):**
```bash
kubectl drain $NODE_NAME --ignore-daemonsets --delete-emptydir-data
```

**Terminate and replace the node (AWS Auto Scaling Group):**
```bash
aws ec2 terminate-instances --instance-ids $(
  kubectl get node $NODE_NAME -o jsonpath='{.spec.providerID}' | cut -d/ -f5
)
```
The ASG will automatically launch a replacement node.

---

## Resolution Checklist

- [ ] Disk usage below 70% on all nodes
- [ ] No pods in evicted state
- [ ] Root cause identified (log explosion, large image, etc.)
- [ ] Log rotation configured if it was the cause
