# ml/drift-detection/drift_detector.py
#
# Model Drift Detection — observability for the ML layer itself.
# "Who watches the watchmen?"
#
# Two types of drift we care about:
#   1. Data drift   — input distribution has shifted (traffic patterns changed)
#                     Model was trained on old patterns, now seeing new ones
#   2. Concept drift — relationship between inputs and outputs has changed
#                      e.g. what used to be "normal" latency is now "high"
#
# Detectors:
#   - PSI  (Population Stability Index) — industry standard for data drift
#   - KS   (Kolmogorov-Smirnov test)    — distribution shift
#   - ADWIN (Adaptive Windowing)        — concept drift in prediction accuracy
#
# When drift is detected:
#   - Alert fires
#   - Model is flagged for retraining
#   - Predictions are labelled with drift warning in Prometheus

import os
import json
import time
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta
from dataclasses import dataclass
from scipy import stats

import redis
import structlog
from prometheus_client import Gauge, Counter, Histogram, start_http_server

log = structlog.get_logger().bind(service="drift-detector")

PROMETHEUS_URL   = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
REDIS_URL        = os.getenv("REDIS_URL", "redis://redis:6379")
SLACK_WEBHOOK    = os.getenv("SLACK_WEBHOOK_URL", "")
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL_SECONDS", "3600"))   # hourly

PSI_WARNING      = 0.1    # PSI > 0.1 → moderate drift, monitor
PSI_CRITICAL     = 0.2    # PSI > 0.2 → significant drift, retrain
KS_P_VALUE       = 0.05   # KS p-value < 0.05 → distributions differ

# ── Metrics ───────────────────────────────────────────────────────────────────

drift_psi = Gauge(
    "ml_drift_psi_score",
    "Population Stability Index score (0=stable, >0.2=retrain)",
    ["model", "feature"],
)
drift_ks_statistic = Gauge(
    "ml_drift_ks_statistic",
    "KS test statistic for distribution comparison",
    ["model", "feature"],
)
drift_detected = Counter(
    "ml_drift_detected_total",
    "Total drift detection events",
    ["model", "drift_type", "severity"],
)
model_retrains_triggered = Counter(
    "ml_retrains_triggered_total",
    "Times a model was flagged for retraining due to drift",
    ["model"],
)
adwin_error_rate = Gauge(
    "ml_adwin_error_rate",
    "ADWIN sliding window error rate for concept drift",
    ["model"],
)
drift_alert_active = Gauge(
    "ml_drift_alert_active",
    "1 if drift alert is currently active for this model",
    ["model"],
)


@dataclass
class DriftReport:
    model:        str
    feature:      str
    timestamp:    str
    psi:          float
    ks_statistic: float
    ks_p_value:   float
    drift_type:   str    # none / data / concept / both
    severity:     str    # ok / warning / critical
    action:       str    # none / monitor / retrain


# ── PSI (Population Stability Index) ─────────────────────────────────────────

def compute_psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    """
    PSI measures how much the distribution of a feature has shifted.
    Reference = training data distribution.
    Current   = recent production data distribution.

    PSI < 0.1  → no significant change
    PSI 0.1-0.2 → moderate change, monitor
    PSI > 0.2  → significant change, retrain model
    """
    if len(reference) < 20 or len(current) < 20:
        return 0.0

    # Bin boundaries from reference distribution
    breakpoints = np.percentile(reference, np.linspace(0, 100, bins + 1))
    breakpoints = np.unique(breakpoints)
    if len(breakpoints) < 3:
        return 0.0

    ref_counts, _ = np.histogram(reference, bins=breakpoints)
    cur_counts, _ = np.histogram(current, bins=breakpoints)

    # Normalise to proportions, avoid zero
    ref_pct = np.maximum(ref_counts / len(reference), 1e-6)
    cur_pct = np.maximum(cur_counts / len(current),   1e-6)

    psi = np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct))
    return float(psi)


# ── ADWIN (Adaptive Windowing) ────────────────────────────────────────────────

class ADWIN:
    """
    Lightweight ADWIN implementation for concept drift detection.
    Maintains an adaptive window of recent prediction errors.
    Fires when mean error in recent window differs significantly from older window.
    """

    def __init__(self, delta: float = 0.002):
        self.delta  = delta
        self.window = []
        self.drift  = False

    def add(self, value: float) -> bool:
        """Add a new value. Returns True if drift detected."""
        self.window.append(value)
        self.drift = self._test_drift()
        return self.drift

    def _test_drift(self) -> bool:
        n = len(self.window)
        if n < 30:
            return False

        # Test all cut points in the window
        for i in range(10, n - 10):
            w0 = np.array(self.window[:i])
            w1 = np.array(self.window[i:])

            m0, m1 = w0.mean(), w1.mean()
            n0, n1 = len(w0), len(w1)

            # Hoeffding bound for drift detection
            epsilon_cut = np.sqrt(
                (1 / (2 * n0) + 1 / (2 * n1)) * np.log(4 * n / self.delta)
            )

            if abs(m0 - m1) >= epsilon_cut:
                # Drift detected — shrink window to more recent data
                self.window = self.window[i:]
                return True

        return False

    @property
    def mean(self) -> float:
        return float(np.mean(self.window)) if self.window else 0.0


# ── Per-model drift monitoring ────────────────────────────────────────────────

class ModelDriftMonitor:
    """Monitors one ML model for both data and concept drift."""

    def __init__(self, model_name: str):
        self.model_name   = model_name
        self.adwin        = ADWIN()
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)

    def get_reference_distribution(self, feature: str) -> np.ndarray:
        """Load training-time distribution from Redis (stored during model training)."""
        key  = f"ml:drift:reference:{self.model_name}:{feature}"
        data = self.redis_client.get(key)
        if data:
            return np.array(json.loads(data))
        return np.array([])

    def get_current_distribution(self, query: str, hours: int = 24) -> np.ndarray:
        """Fetch recent production values from Prometheus."""
        end   = datetime.utcnow()
        start = end - timedelta(hours=hours)
        try:
            resp = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={
                    "query": query,
                    "start": start.timestamp(),
                    "end":   end.timestamp(),
                    "step":  "300",
                },
                timeout=15,
            )
            result = resp.json()["data"]["result"]
            if result:
                return np.array([float(v[1]) for v in result[0]["values"]])
            return np.array([])
        except Exception as e:
            log.error("prometheus fetch failed", model=self.model_name, error=str(e))
            return np.array([])

    def store_reference_distribution(self, feature: str, data: np.ndarray) -> None:
        """Called at training time to store the reference distribution."""
        key = f"ml:drift:reference:{self.model_name}:{feature}"
        self.redis_client.setex(key, 60 * 60 * 24 * 180, json.dumps(data.tolist()))

    def check(self, feature: str, query: str) -> DriftReport:
        reference = self.get_reference_distribution(feature)
        current   = self.get_current_distribution(query)

        if len(reference) < 20 or len(current) < 20:
            return DriftReport(
                model=self.model_name, feature=feature,
                timestamp=datetime.utcnow().isoformat(),
                psi=0.0, ks_statistic=0.0, ks_p_value=1.0,
                drift_type="none", severity="ok", action="none",
            )

        # PSI
        psi = compute_psi(reference, current)

        # KS test
        ks_stat, ks_p = stats.ks_2samp(reference, current)

        # ADWIN — feed recent prediction errors (if available)
        # For now use normalised current values as proxy for prediction error
        if len(current) > 0:
            normalised = (current - reference.mean()) / (reference.std() + 1e-8)
            for val in normalised[-10:]:
                self.adwin.add(abs(val))

        adwin_error_rate.labels(model=self.model_name).set(self.adwin.mean)

        # Publish metrics
        drift_psi.labels(model=self.model_name, feature=feature).set(psi)
        drift_ks_statistic.labels(model=self.model_name, feature=feature).set(ks_stat)

        # Determine severity
        if psi > PSI_CRITICAL or (ks_p < KS_P_VALUE and self.adwin.drift):
            severity   = "critical"
            action     = "retrain"
            drift_type = "both" if self.adwin.drift else "data"
        elif psi > PSI_WARNING or ks_p < KS_P_VALUE:
            severity   = "warning"
            action     = "monitor"
            drift_type = "data"
        elif self.adwin.drift:
            severity   = "warning"
            action     = "monitor"
            drift_type = "concept"
        else:
            severity   = "ok"
            action     = "none"
            drift_type = "none"

        if severity != "ok":
            drift_detected.labels(
                model=self.model_name,
                drift_type=drift_type,
                severity=severity,
            ).inc()
            drift_alert_active.labels(model=self.model_name).set(1)
        else:
            drift_alert_active.labels(model=self.model_name).set(0)

        if action == "retrain":
            model_retrains_triggered.labels(model=self.model_name).inc()
            self._flag_for_retrain()

        return DriftReport(
            model=self.model_name, feature=feature,
            timestamp=datetime.utcnow().isoformat(),
            psi=round(psi, 4),
            ks_statistic=round(float(ks_stat), 4),
            ks_p_value=round(float(ks_p), 4),
            drift_type=drift_type,
            severity=severity,
            action=action,
        )

    def _flag_for_retrain(self) -> None:
        """Write retrain flag to Redis — forecaster/detector picks this up."""
        key = f"ml:retrain-needed:{self.model_name}"
        self.redis_client.setex(key, 3600, "1")
        log.warning("model flagged for retraining", model=self.model_name)

        if SLACK_WEBHOOK:
            try:
                requests.post(SLACK_WEBHOOK, json={
                    "attachments": [{
                        "color": "warning",
                        "title": f"🔄 ML Model Drift Detected: {self.model_name}",
                        "text": (
                            f"Model `{self.model_name}` has significant data drift "
                            f"and has been flagged for retraining. "
                            f"Predictions may be less reliable until retrained."
                        ),
                        "footer": "Drift Detector | Autonomous Observability Platform",
                    }]
                }, timeout=5)
            except Exception:
                pass


# ── Monitor registry ──────────────────────────────────────────────────────────

MODELS_TO_MONITOR = [
    {
        "model":   "traffic-forecaster-order-service",
        "feature": "request_rate",
        "query":   "sum(rate(order_service_requests_total[5m]))",
    },
    {
        "model":   "traffic-forecaster-payment-service",
        "feature": "request_rate",
        "query":   "sum(rate(payment_service_requests_total[5m]))",
    },
    {
        "model":   "anomaly-detector-error-rate",
        "feature": "error_rate",
        "query":   "sum(rate(order_service_requests_total{status_code=~'5..'}[5m])) / sum(rate(order_service_requests_total[5m]))",
    },
    {
        "model":   "anomaly-detector-latency",
        "feature": "latency_p95",
        "query":   "histogram_quantile(0.95, sum by (le) (rate(order_service_request_duration_seconds_bucket[5m])))",
    },
]


def run_drift_detection_loop():
    start_http_server(8003)
    log.info("drift detector starting", models=len(MODELS_TO_MONITOR))

    monitors = {
        cfg["model"]: ModelDriftMonitor(cfg["model"])
        for cfg in MODELS_TO_MONITOR
    }

    while True:
        for cfg in MODELS_TO_MONITOR:
            model   = cfg["model"]
            feature = cfg["feature"]
            query   = cfg["query"]
            monitor = monitors[model]

            try:
                report = monitor.check(feature, query)
                log.info("drift check complete",
                    model=model,
                    psi=report.psi,
                    severity=report.severity,
                    action=report.action,
                )
            except Exception as e:
                log.error("drift check failed", model=model, error=str(e))

        log.info(f"drift check cycle complete — sleeping {CHECK_INTERVAL}s")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_drift_detection_loop()
