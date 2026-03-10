# Runbook: Kafka Consumer Lag

**Alert:** `KafkaConsumerLagHigh` / `KafkaConsumerLagCritical`
**Severity:** Warning / Critical
**SLO Impact:** Tier 2 — Kafka consumer lag SLO (< 30 seconds)
**Last Updated:** 2024-01-01

---

## What Is Happening

A Kafka consumer group is falling behind the producer rate. Notifications and/or analytics are delayed. If lag reaches critical levels, users may experience delayed notifications and stale analytics data.

---

## Immediate Actions (First 5 Minutes)

**1. Identify which consumer group and topic**
```promql
# Lag by consumer group and topic
kafka_consumergroup_lag
```
Or via CLI:
```bash
kubectl exec -n app deployment/kafka -- \
  kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --describe --all-groups
```

**2. Check consumer pod health**
```bash
# Notification service consumer
kubectl get pods -n app -l app=notification-service
kubectl logs -n app deployment/notification-service --tail=100

# Analytics service consumer
kubectl get pods -n app -l app=analytics-service
kubectl logs -n app deployment/analytics-service --tail=100
```

**3. Check if producers are spiking**
```promql
# Producer message rate
sum(rate(kafka_server_brokertopicmetrics_messagesin_total[5m])) by (topic)
```
If producer rate has spiked → consumer can't keep up → scale out consumers.

---

## Diagnosis Decision Tree

```
Kafka consumer lag high
│
├── Consumer pods CrashLooping? ────────────────────► Fix pod health first
│   kubectl get pods -n app                            Check logs for errors
│
├── Consumer pods healthy but lag growing? ─────────► Consumer is too slow
│   (pods running, lag increasing)                     Scale out consumer pods
│                                                       Check processing latency
│
├── Producer rate spiked (traffic spike)? ──────────► Temporary — lag will clear
│   (check producer metrics)                           Scale consumers if sustained
│
├── Kafka broker unhealthy? ─────────────────────────► Follow broker recovery
│   kubectl get pods -n app -l app=kafka               May need broker restart
│
└── Consumer group stuck / rebalancing? ────────────► Force rebalance
    (lag not moving at all)                             Restart consumer pods
                                                         kubectl rollout restart
                                                          deployment/notification-service -n app
```

---

## Resolution Actions

**Scale out consumers (most common fix):**
```bash
kubectl scale deployment/notification-service --replicas=4 -n app
kubectl scale deployment/analytics-service --replicas=4 -n app
```

**Force consumer group rebalance:**
```bash
kubectl rollout restart deployment/notification-service -n app
kubectl rollout restart deployment/analytics-service -n app
```

**Check consumer group offsets manually:**
```bash
kubectl exec -n app deployment/kafka -- \
  kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --group notification-service \
  --describe
```

**Reset consumer offsets to latest (LAST RESORT — skips messages):**
```bash
# Only if messages are unrecoverable and lag must be cleared immediately
kubectl exec -n app deployment/kafka -- \
  kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --group notification-service \
  --topic orders \
  --reset-offsets \
  --to-latest \
  --execute
```
⚠️ This skips unprocessed messages. Users will NOT receive notifications for skipped orders. Only use if ordered to by on-call lead.

---

## Resolution Checklist

- [ ] Consumer lag below 30 second SLO threshold
- [ ] All consumer groups showing steady or decreasing lag
- [ ] Root cause documented (traffic spike / pod crash / broker issue)
- [ ] Any offset resets documented with justification
- [ ] Scaling changes committed to manifests if permanent
