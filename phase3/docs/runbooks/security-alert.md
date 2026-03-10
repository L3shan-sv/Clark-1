# Runbook: Security Alert

**Alert:** `FalcoSecurityAlert` / `FalcoPrivilegeEscalation` / `FalcoUnexpectedOutboundConnection`
**Severity:** Critical (privilege escalation) / Warning (others)
**SLO Impact:** Security posture — not SLO but treated as P1
**Last Updated:** 2024-01-01

---

## What Is Happening

Falco has detected suspicious runtime behavior in a container. This could indicate:
- A compromised container attempting to escalate privileges
- Unexpected data exfiltration (unexpected outbound connection)
- Container escape attempt
- Cryptomining or other malicious workload

**Treat every Falco alert as real until proven otherwise.**

---

## Immediate Actions (First 5 Minutes)

**1. Identify the alert details**
```bash
# Falco logs — see the raw alert
kubectl logs -n security deployment/falco --tail=50 | grep -i "priority: Critical\|priority: Warning"
```

Check Grafana → Security Dashboard for the full alert timeline.

**2. Identify the affected pod**
The Falco alert will contain:
- `container.name` — the container
- `k8s.pod.name` — the pod
- `k8s.ns.name` — the namespace
- `proc.cmdline` — the command that triggered the rule

**3. Immediately isolate the pod (network isolation)**
```bash
# Apply a restrictive NetworkPolicy to the pod
kubectl label pod <pod-name> -n <namespace> security=quarantine

# Apply deny-all policy to quarantined pods
kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: quarantine
  namespace: <namespace>
spec:
  podSelector:
    matchLabels:
      security: quarantine
  policyTypes:
    - Ingress
    - Egress
EOF
```

**4. Capture forensic evidence BEFORE killing the pod**
```bash
POD=<pod-name>
NS=<namespace>

# Capture running processes
kubectl exec -n $NS $POD -- ps aux > /tmp/forensics-processes.txt

# Capture network connections
kubectl exec -n $NS $POD -- ss -tlnp > /tmp/forensics-network.txt

# Capture environment variables (may contain stolen credentials)
kubectl exec -n $NS $POD -- env > /tmp/forensics-env.txt

# Capture recent file modifications
kubectl exec -n $NS $POD -- find / -newer /proc/1 -type f 2>/dev/null \
  > /tmp/forensics-modified-files.txt
```

**5. Kill the pod**
```bash
kubectl delete pod <pod-name> -n <namespace> --grace-period=0 --force
```

---

## Alert-Specific Actions

**Privilege Escalation (`setuid`, `setgid`, capability changes):**
- Immediate pod kill — no waiting
- Rotate ALL secrets that pod had access to
- Check if the container image has been tampered with
- Pull the image digest and verify against known-good digest

**Unexpected Outbound Connection:**
- Check destination IP against threat intel
- Check what data was in the connection (eBPF/Tetragon logs)
- If data exfiltration suspected — notify security team and legal

**Shell Spawned in Container:**
- Containers should never need a shell in production
- Could be an attacker or a developer who SSHed in manually
- Either way — isolate, investigate, kill

---

## Post-Incident

- [ ] Pod killed and replaced from clean image
- [ ] All secrets rotated that the pod had access to
- [ ] Container image re-scanned with Grype
- [ ] Vault dynamic secret paths audited for access
- [ ] Security team notified
- [ ] Postmortem within 24 hours (security incidents are P1)
- [ ] Falco rule tuned if false positive confirmed
