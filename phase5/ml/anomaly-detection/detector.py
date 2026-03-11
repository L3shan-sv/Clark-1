# ml/anomaly-detection/detector.py
#
# Ensemble anomaly detector — three models run in parallel:
#   1. Isolation Forest  → point anomalies (sudden spikes/drops)
#   2. LSTM Autoencoder  → temporal/seasonal anomalies (gradual drift)
#   3. CUSUM             → slow drift (consistently off from baseline)
#
# Ensemble layer combines scores with confidence weighting.
# Only alerts when ensemble confidence exceeds threshold.
# This is what eliminates the false positive storm of static thresholds.

import os
import json
import time
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta
from typing import Optional
from collections import deque
from dataclasses import dataclass, asdict

import structlog
from prometheus_client import Gauge, Counter, Histogram, start_http_server
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

log = structlog.get_logger().bind(service="anomaly-detector")

PROMETHEUS_URL     = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
REDIS_URL          = os.getenv("REDIS_URL", "redis://redis:6379")
ALERT_WEBHOOK      = os.getenv("ALERT_WEBHOOK", "http://argo-eventsource:12000/anomaly-detected")
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.75"))
SCAN_INTERVAL      = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))

# ── Metrics ───────────────────────────────────────────────────────────────────

anomalies_detected = Counter(
    "ml_anomalies_detected_total",
    "Total anomalies detected by the ensemble",
    ["service", "metric", "detector"],
)
ensemble_confidence = Gauge(
    "ml_ensemble_confidence",
    "Current ensemble confidence score for anomaly",
    ["service", "metric"],
)
false_positive_rate = Gauge(
    "ml_false_positive_rate",
    "Estimated false positive rate per detector",
    ["detector"],
)
detector_score = Gauge(
    "ml_detector_score",
    "Individual detector anomaly score (-1 to 1)",
    ["service", "metric", "detector"],
)


@dataclass
class AnomalyResult:
    service:        str
    metric:         str
    timestamp:      str
    is_anomaly:     bool
    confidence:     float
    severity:       str           # low / medium / high / critical
    detectors_fired: list
    current_value:  float
    baseline_mean:  float
    deviation_pct:  float
    description:    str


# ── Detector 1: Isolation Forest ──────────────────────────────────────────────

class IsolationForestDetector:
    """
    Good at: point anomalies — sudden spikes, drops, unexpected values.
    Trains on 7 days of data. Contamination = expected anomaly rate.
    Score: -1 (anomaly) to 1 (normal). We normalise to 0-1.
    """

    def __init__(self, contamination: float = 0.05):
        self.contamination = contamination
        self.model         = IsolationForest(
            contamination=contamination,
            n_estimators=100,
            random_state=42,
        )
        self.scaler  = StandardScaler()
        self.trained = False

    def fit(self, data: np.ndarray) -> None:
        scaled = self.scaler.fit_transform(data.reshape(-1, 1))
        self.model.fit(scaled)
        self.trained = True

    def score(self, value: float) -> float:
        """Returns anomaly probability 0-1. Higher = more anomalous."""
        if not self.trained:
            return 0.0
        scaled     = self.scaler.transform([[value]])
        raw_score  = self.model.decision_function(scaled)[0]
        # Convert: more negative = more anomalous → invert and normalise
        normalised = 1 / (1 + np.exp(raw_score * 5))
        return float(normalised)


# ── Detector 2: LSTM Autoencoder ─────────────────────────────────────────────

class LSTMAutoencoderDetector:
    """
    Good at: temporal/seasonal anomalies — patterns that deviate from
    what's expected given the time of day and day of week.
    Reconstruction error = how "surprising" this sequence is.
    """

    def __init__(self, sequence_length: int = 12, threshold_multiplier: float = 3.0):
        self.sequence_length     = sequence_length   # 12 × 5min = 1 hour context
        self.threshold_multiplier = threshold_multiplier
        self.threshold:  Optional[float] = None
        self.model:      Optional[object] = None
        self.trained     = False

    def _build_model(self, n_features: int = 1):
        """Build LSTM autoencoder architecture."""
        try:
            import tensorflow as tf
            from tensorflow.keras.models import Model
            from tensorflow.keras.layers import (
                Input, LSTM, RepeatVector, TimeDistributed, Dense
            )

            inputs  = Input(shape=(self.sequence_length, n_features))
            # Encoder
            encoded = LSTM(32, activation="relu", return_sequences=False)(inputs)
            # Bottleneck
            bottleneck = Dense(8, activation="relu")(encoded)
            # Decoder
            repeated = RepeatVector(self.sequence_length)(bottleneck)
            decoded  = LSTM(32, activation="relu", return_sequences=True)(repeated)
            output   = TimeDistributed(Dense(n_features))(decoded)

            model = Model(inputs, output)
            model.compile(optimizer="adam", loss="mse")
            return model
        except ImportError:
            log.warning("TensorFlow not available — LSTM detector disabled")
            return None

    def _make_sequences(self, data: np.ndarray) -> np.ndarray:
        sequences = []
        for i in range(len(data) - self.sequence_length):
            sequences.append(data[i:i + self.sequence_length])
        return np.array(sequences).reshape(-1, self.sequence_length, 1)

    def fit(self, data: np.ndarray) -> None:
        if len(data) < self.sequence_length * 2:
            return

        self.model = self._build_model()
        if self.model is None:
            return

        # Normalise
        self.mean_ = data.mean()
        self.std_  = data.std() + 1e-8
        normalised = (data - self.mean_) / self.std_

        sequences = self._make_sequences(normalised)
        self.model.fit(
            sequences, sequences,
            epochs=20,
            batch_size=32,
            validation_split=0.1,
            verbose=0,
        )

        # Set threshold from training reconstruction error
        preds  = self.model.predict(sequences, verbose=0)
        errors = np.mean(np.square(sequences - preds), axis=(1, 2))
        self.threshold = errors.mean() + self.threshold_multiplier * errors.std()
        self.trained   = True

    def score(self, recent_window: np.ndarray) -> float:
        """Score a recent sequence. Returns 0-1 anomaly probability."""
        if not self.trained or self.model is None:
            return 0.0
        if len(recent_window) < self.sequence_length:
            return 0.0

        normalised = (recent_window[-self.sequence_length:] - self.mean_) / self.std_
        seq        = normalised.reshape(1, self.sequence_length, 1)
        pred       = self.model.predict(seq, verbose=0)
        error      = float(np.mean(np.square(seq - pred)))

        # Normalise error to 0-1 using threshold
        score = min(1.0, error / (self.threshold + 1e-8))
        return score


# ── Detector 3: CUSUM ─────────────────────────────────────────────────────────

class CUSUMDetector:
    """
    Cumulative Sum — detects persistent slow drift from baseline.
    Perfect for: gradual memory leak, slow query degradation, subtle data issues.
    Things that wouldn't trigger a single-point anomaly detector.
    """

    def __init__(self, k: float = 0.5, h: float = 5.0):
        self.k      = k    # Slack parameter — how much deviation to ignore
        self.h      = h    # Decision threshold
        self.s_pos  = 0.0  # Upper CUSUM statistic
        self.s_neg  = 0.0  # Lower CUSUM statistic
        self.mean_  = None
        self.std_   = None

    def fit(self, data: np.ndarray) -> None:
        self.mean_ = data.mean()
        self.std_  = data.std() + 1e-8
        # Reset statistics
        self.s_pos = 0.0
        self.s_neg = 0.0

    def update(self, value: float) -> float:
        """Update CUSUM with new value. Returns anomaly score 0-1."""
        if self.mean_ is None:
            return 0.0

        z = (value - self.mean_) / self.std_

        # Update upper and lower CUSUM
        self.s_pos = max(0, self.s_pos + z - self.k)
        self.s_neg = max(0, self.s_neg - z - self.k)

        # Score based on how much threshold is exceeded
        max_s = max(self.s_pos, self.s_neg)
        score = min(1.0, max_s / self.h)
        return score


# ── Ensemble ──────────────────────────────────────────────────────────────────

class EnsembleDetector:
    """
    Combines all three detectors with confidence weighting.
    Each detector gets a weight based on its recent false positive rate.
    Only fires when combined confidence > CONFIDENCE_THRESHOLD.
    """

    # Weights — adjusted by track record
    WEIGHTS = {
        "isolation_forest": 0.35,
        "lstm":             0.40,
        "cusum":            0.25,
    }

    def __init__(self, service: str, metric: str):
        self.service  = service
        self.metric   = metric
        self.iso      = IsolationForestDetector()
        self.lstm     = LSTMAutoencoderDetector()
        self.cusum    = CUSUMDetector()
        self.window   = deque(maxlen=500)   # Rolling window for LSTM
        self.trained  = False

    def fit(self, data: np.ndarray) -> None:
        self.iso.fit(data)
        self.lstm.fit(data)
        self.cusum.fit(data)
        self.trained = True
        log.info("ensemble trained",
            service=self.service,
            metric=self.metric,
            data_points=len(data),
        )

    def score(self, value: float) -> AnomalyResult:
        self.window.append(value)

        # Get individual scores
        iso_score   = self.iso.score(value)
        lstm_score  = self.lstm.score(np.array(list(self.window)))
        cusum_score = self.cusum.update(value)

        # Publish individual scores to Prometheus
        detector_score.labels(self.service, self.metric, "isolation_forest").set(iso_score)
        detector_score.labels(self.service, self.metric, "lstm").set(lstm_score)
        detector_score.labels(self.service, self.metric, "cusum").set(cusum_score)

        # Weighted ensemble
        confidence = (
            self.WEIGHTS["isolation_forest"] * iso_score +
            self.WEIGHTS["lstm"]             * lstm_score +
            self.WEIGHTS["cusum"]            * cusum_score
        )

        ensemble_confidence.labels(self.service, self.metric).set(confidence)

        is_anomaly      = confidence >= CONFIDENCE_THRESHOLD
        detectors_fired = []
        if iso_score > 0.6:   detectors_fired.append("isolation_forest")
        if lstm_score > 0.6:  detectors_fired.append("lstm")
        if cusum_score > 0.6: detectors_fired.append("cusum")

        if is_anomaly:
            anomalies_detected.labels(self.service, self.metric, "ensemble").inc()

        baseline_mean  = float(np.mean(list(self.window)[:-1])) if len(self.window) > 1 else value
        deviation_pct  = abs(value - baseline_mean) / (baseline_mean + 1e-8) * 100

        severity = (
            "critical" if confidence > 0.90 else
            "high"     if confidence > 0.80 else
            "medium"   if confidence > 0.70 else
            "low"
        )

        return AnomalyResult(
            service        = self.service,
            metric         = self.metric,
            timestamp      = datetime.utcnow().isoformat(),
            is_anomaly     = is_anomaly,
            confidence     = round(confidence, 3),
            severity       = severity,
            detectors_fired = detectors_fired,
            current_value  = round(value, 4),
            baseline_mean  = round(baseline_mean, 4),
            deviation_pct  = round(deviation_pct, 2),
            description    = (
                f"{self.metric} on {self.service} is {deviation_pct:.1f}% "
                f"from baseline. Detectors: {', '.join(detectors_fired) or 'none'}. "
                f"Confidence: {confidence:.1%}."
            ),
        )


# ── Main Detection Loop ───────────────────────────────────────────────────────

METRICS_TO_MONITOR = [
    ("order-service",   "sum(rate(order_service_requests_total[5m]))"),
    ("order-service",   "sum(rate(order_service_requests_total{status_code=~'5..'}[5m])) / sum(rate(order_service_requests_total[5m]))"),
    ("payment-service", "sum(rate(payment_service_requests_total{status_code=~'5..'}[5m])) / sum(rate(payment_service_requests_total[5m]))"),
    ("order-service",   "histogram_quantile(0.95, sum by (le) (rate(order_service_request_duration_seconds_bucket[5m])))"),
]


def fetch_current_value(query: str) -> Optional[float]:
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        result = resp.json()["data"]["result"]
        if result:
            return float(result[0]["value"][1])
        return None
    except Exception as e:
        log.error("prometheus query failed", query=query[:50], error=str(e))
        return None


def fetch_historical_data(query: str, days: int = 7) -> np.ndarray:
    end   = datetime.utcnow()
    start = end - timedelta(days=days)
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={
                "query": query,
                "start": start.timestamp(),
                "end":   end.timestamp(),
                "step":  "300",
            },
            timeout=30,
        )
        data = resp.json()["data"]["result"]
        if data:
            values = [float(v[1]) for v in data[0]["values"]]
            return np.array(values)
        return np.array([])
    except Exception:
        return np.array([])


def fire_anomaly_alert(result: AnomalyResult) -> None:
    """POST anomaly to Argo EventSource for automated response."""
    try:
        requests.post(
            ALERT_WEBHOOK,
            json={
                "status":      "firing",
                "alerts": [{
                    "labels": {
                        "alertname": "AnomalyDetected",
                        "service":   result.service,
                        "metric":    result.metric,
                        "severity":  result.severity,
                    },
                    "annotations": {
                        "description": result.description,
                        "confidence":  str(result.confidence),
                    },
                }],
            },
            timeout=5,
        )
        log.info("anomaly alert fired",
            service=result.service,
            metric=result.metric,
            confidence=result.confidence,
            severity=result.severity,
        )
    except Exception as e:
        log.error("failed to fire anomaly alert", error=str(e))


def run_detection_loop():
    start_http_server(8002)
    log.info("anomaly detector starting",
        metrics=len(METRICS_TO_MONITOR),
        confidence_threshold=CONFIDENCE_THRESHOLD,
    )

    # Initialise and train one ensemble per (service, metric)
    detectors = {}
    for service, query in METRICS_TO_MONITOR:
        key     = f"{service}:{query[:30]}"
        det     = EnsembleDetector(service=service, metric=query[:50])
        history = fetch_historical_data(query, days=7)
        if len(history) > 50:
            det.fit(history)
        detectors[key] = (det, query)

    log.info("all detectors initialised", count=len(detectors))

    while True:
        for key, (detector, query) in detectors.items():
            value = fetch_current_value(query)
            if value is None:
                continue

            result = detector.score(value)

            if result.is_anomaly:
                log.warning("anomaly detected",
                    service=result.service,
                    confidence=result.confidence,
                    severity=result.severity,
                    deviation_pct=result.deviation_pct,
                    detectors=result.detectors_fired,
                )
                fire_anomaly_alert(result)

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    run_detection_loop()
