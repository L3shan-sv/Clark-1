# Runbook: Redis Memory Pressure

**Alert:** `RedisMemoryPressureHigh`
**Severity:** Warning (> 75%) / Critical (> 90%)
**SLO Impact:** Tier 2 — Redis availability SLO
**Last Updated:** 2024-01-01

---

## What Is Happening

Redis memory usage is approaching its configured `maxmemory` limit. When Redis hits the limit, it will either reject writes (if policy is `noeviction`) or start evicting keys (if policy is `allkeys-lru`). Either outcome can cause application errors.

---

## Immediate Actions (First 5 Minutes)

**1. Check current memory usage**
```promql
# Memory used vs max
redis_memory_used_bytes / redis_memory_max_bytes
```

```bash
kubectl exec -n app deployment/redis -- redis-cli info memory | grep -E "used_memory_human|maxmemory_human|mem_fragmentation_ratio"
```

**2. Check eviction rate**
```promql
rate(redis_evicted_keys_total[5m])
```
If eviction rate > 0 — keys are already being evicted. Check if applications are seeing cache misses.

**3. Check current eviction policy**
```bash
kubectl exec -n app deployment/redis -- redis-cli config get maxmemory-policy
```

**4. Find the biggest key consumers**
```bash
kubectl exec -n app deployment/redis -- redis-cli --bigkeys
```

---

## Diagnosis Decision Tree

```
Redis memory > 85%
│
├── Eviction rate > 0 AND policy = noeviction? ──────► CRITICAL
│                                                        Writes will start failing
│                                                        Change policy immediately (step below)
│
├── Memory growing steadily (not a spike)? ─────────► Key TTL misconfiguration
│                                                        Keys not expiring as expected
│                                                        Audit TTL on order and payment keys
│
├── Memory spiked suddenly? ─────────────────────────► Traffic spike or bug
│                                                        Check order creation rate
│                                                        Check for keys without TTL
│
└── Fragmentation ratio > 1.5? ─────────────────────► Memory fragmentation
    redis_mem_fragmentation_ratio > 1.5                 Schedule Redis restart
                                                          during low traffic window
```

---

## Mitigation Actions

**Switch eviction policy to LRU (safe — evicts least recently used keys):**
```bash
kubectl exec -n app deployment/redis -- redis-cli config set maxmemory-policy allkeys-lru
```
This is safe for our use case — order state is also in PostgreSQL.

**Increase Redis memory limit (if consistently near threshold):**
```bash
kubectl exec -n app deployment/redis -- redis-cli config set maxmemory 512mb
```
Also update the Kubernetes resource limits in the manifest and commit.

**Manually flush expired keys:**
```bash
# Scan and delete keys older than expected TTL
kubectl exec -n app deployment/redis -- redis-cli --scan --pattern "order:*" | head -20
# Check a specific key TTL
kubectl exec -n app deployment/redis -- redis-cli ttl "order:some-uuid"
```

**Emergency: flush all keys (LAST RESORT — causes cache miss storm):**
```bash
# Only if Redis is completely full and writes are failing
kubectl exec -n app deployment/redis -- redis-cli flushdb async
```
⚠️ This clears all data. Services will experience cache misses. Order state will be recovered from PostgreSQL. Notify on-call lead before executing.

---

## Prevention

- All Redis keys MUST have a TTL — enforce this in code review
- Order keys: 24h TTL
- Payment keys: 30 day TTL
- Session keys: 7 day TTL
- Set Redis memory alert threshold at 75% to give 25% headroom

---

## Resolution Checklist

- [ ] Memory usage below 80%
- [ ] Eviction rate back to 0 (or acceptable level)
- [ ] Root cause identified (TTL missing, traffic spike, etc.)
- [ ] maxmemory config updated in manifest if limit was changed
- [ ] Any key TTL bugs fixed and deployed
