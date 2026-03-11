# Phase 5 — ML & Intelligence Layer

![Architecture](../docs/images/phase5-architecture.svg)

## Overview

Phase 5 replaces reactive alerting with proactive intelligence. Five ML models work in concert: a forecaster predicts traffic 30 minutes ahead, an ensemble detector catches anomalies before they breach SLOs, a causal engine identifies the true root cause in under 90 seconds, a reinforcement learning agent selects the optimal remediation, and a drift detector ensures all models stay accurate over time.

---

## The Five Models

### 1. Traffic Forecaster (`ml/traffic-forecasting/forecaster.py`)
**Algorithm:** Facebook Prophet  
**Purpose:** Predict request rate 30–60 minutes ahead so KEDA can scale pods *before* traffic arrives.

```
Data source:   Prometheus (90 days of RPS metrics)
Retrain:       Every hour (strips outliers before fitting)
Output:        ml_forecast_upper_rps{service} → Redis
Validation:    7-day cross-validation, MAPE tracked as ml_forecast_mape_percent
Action:        KEDA reads upper confidence bound → adjusts minReplicas
```

Why Prophet? It natively models daily + weekly seasonality without feature engineering. The model trains in < 10 seconds — retraining hourly is feasible. LSTM would take minutes to retrain.

### 2. Anomaly Detector (`ml/anomaly-detection/detector.py`)
**Algorithm:** Ensemble of three models  
**Purpose:** Detect when a metric is behaving unusually, with high confidence.

| Component | Algorithm | Catches |
|---|---|---|
| Model A | Isolation Forest | Point anomalies (sudden spike/drop) |
| Model B | LSTM Autoencoder | Temporal pattern breaks (unusual shape) |
| Model C | CUSUM | Slow drift (gradual degradation over hours) |

```
Combination:  Weighted ensemble — fires when confidence > 0.75
Output:       ml_anomaly_detected{service, metric, confidence}
Action:       POST to Argo EventSource /anomaly-detected
```

Why an ensemble? Each algorithm has different blind spots. Isolation Forest misses slow trends. CUSUM misses sudden spikes. LSTM misses statistical outliers. Together they cover almost all anomaly types.

### 3. Causal Inference Engine (`ml/causal-inference/causal_engine.py`)
**Algorithm:** Granger causality + dependency graph BFS  
**Purpose:** When an anomaly is detected, find the *real* root cause (not just the most visible symptom).

```
Input:        Anomalous metric (e.g. order_service_error_rate)
Process:
  1. BFS traverse dependency graph upstream from affected service
  2. For each candidate: run Granger causality test on last 30 min of metrics
  3. Rank candidates by: Granger p-value × Pearson correlation × graph distance
Output:       Ranked list with confidence scores
              "payment-service latency is the likely cause (p=0.003, conf=0.91)"
```

Granger causality tests whether past values of metric A predict future values of metric B. Combined with the dependency graph (which services call which), this reliably distinguishes "payment-service caused order-service errors" from "both were affected by the same underlying issue."

### 4. RL Remediation Agent (`ml/rl-agent/rl_agent.py`)
**Algorithm:** Multi-armed bandit with Thompson Sampling  
**Purpose:** Select the best remediation action for each incident class, learning from outcomes over time.

```
State space:  (incident_class, severity, service, time_of_day)
Actions:      [rollback, restart_pods, scale_out, evict_cache, circuit_break, escalate]
Learning:     Beta(successes, failures) per (incident_class, action) pair
Exploration:  Thompson Sampling — sample from Beta distribution each time

Graduation:   Starts in SHADOW_MODE=true
              After 50 observations with > 70% success rate → promotes to AUTO
              Policy persisted to Redis (survives restarts)
```

**Shadow mode is critical.** The agent runs for weeks observing what human engineers do and whether it works, before it's allowed to act. You build confidence before granting autonomy.

### 5. Drift Detector (`ml/drift-detection/drift_detector.py`)
**Algorithm:** PSI + KS test + ADWIN  
**Purpose:** Detect when a model's training distribution no longer matches production data.

```
PSI (Population Stability Index):
  PSI < 0.1  → no significant change (green)
  PSI 0.1–0.2 → moderate change, monitor (yellow)
  PSI > 0.2  → significant drift, retrain (red) → flag written to Redis

KS test:    Two-sample Kolmogorov-Smirnov — distribution shape change
ADWIN:      Adaptive Windowing — concept drift in non-stationary streams

Frequency:  Checks every hour
On drift:   Slack alert + retrain flag in Redis + ml_drift_psi_score metric
```

---

## Files

```
phase5/
├── ml/
│   ├── traffic-forecasting/
│   │   └── forecaster.py
│   ├── anomaly-detection/
│   │   └── detector.py
│   ├── causal-inference/
│   │   └── causal_engine.py
│   ├── rl-agent/
│   │   └── rl_agent.py
│   ├── drift-detection/
│   │   └── drift_detector.py
│   └── requirements-all.txt
├── kubernetes/
│   └── ml/
│       ├── ml-deployments.yaml       # 5 Deployments on ml node group
│       └── ml-alert-rules.yaml       # Alerts: MAPE high, drift detected, model down
├── dashboards/
│   └── ml-intelligence.json          # Grafana: forecasts, anomaly scores, RL policy
└── scripts/
    └── deploy-phase5.sh
```

---

## Metrics Exposed

Every ML service exposes metrics at `/metrics`:

| Metric | Description |
|---|---|
| `ml_forecast_predicted_rps` | Prophet point forecast |
| `ml_forecast_upper_rps` | Prophet upper confidence bound (used for scaling) |
| `ml_forecast_mape_percent` | Forecast accuracy — alert if > 30% |
| `ml_ensemble_confidence` | Anomaly detector confidence (0.0–1.0) |
| `ml_isolation_forest_score` | Isolation Forest anomaly score |
| `ml_lstm_reconstruction_error` | LSTM reconstruction error |
| `ml_cusum_statistic` | CUSUM test statistic |
| `ml_rl_action_chosen` | Which action the RL agent selected |
| `ml_rl_action_success_rate` | Success rate per action per incident class |
| `ml_drift_psi_score` | PSI drift score — alert if > 0.2 |

---

## Deploy

```bash
cd phase5
bash scripts/deploy-phase5.sh

# RL agent starts in shadow mode automatically
# Check shadow mode status:
kubectl exec -n ml deployment/rl-agent -- \
  curl -s http://localhost:8003/policy | jq '.shadow_mode'
```

---

## Graduating the RL Agent from Shadow to Auto

```bash
# Check readiness (need 50 observations, > 70% success rate)
kubectl exec -n ml deployment/rl-agent -- \
  curl -s http://localhost:8003/policy | jq '.graduation_eligible'

# If ready, approve graduation (human approval required)
kubectl set env deployment/rl-agent -n ml SHADOW_MODE=false
kubectl rollout status deployment/rl-agent -n ml

# Monitor for 24 hours before leaving unattended
kubectl logs -n ml deployment/rl-agent -f | grep "action_taken"
```

---

## Design Decisions

### Why Thompson Sampling over Q-learning?
Incidents are sparse — maybe 5–10 per week. Q-learning needs thousands of samples to converge. Thompson Sampling works with 10–50 samples per (incident, action) pair. The Beta distribution naturally represents uncertainty: if you've seen 3 successes and 1 failure, your Beta(3,1) distribution expresses "probably good, but uncertain." Thompson Sampling exploits this uncertainty optimally.

### Why shadow mode before auto?
The agent could recommend actions that seem statistically good but have subtle failure modes in specific contexts (e.g. rolling back during a database migration window). Shadow mode lets you validate the policy against real incidents for weeks before granting autonomy. You wouldn't give a new hire the ability to push to production on day 1.

### Why Granger causality instead of correlation?
Correlation is symmetric: `corr(A, B) == corr(B, A)`. It tells you nothing about which caused which. Granger causality is directional: "does the past of A predict the future of B, above and beyond what B's own past predicts?" Combined with the service dependency graph, this reliably identifies the upstream cause.

---

## What's Next

[Phase 6 →](../phase6/README.md) — Secure everything with zero trust networking, dynamic secrets, and runtime security monitoring
