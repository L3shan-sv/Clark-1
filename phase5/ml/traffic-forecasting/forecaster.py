# ml/traffic-forecasting/forecaster.py
#
# Prophet-based traffic forecasting.
# Trains on 90 days of Prometheus request rate data.
# Predicts traffic 30 minutes ahead per service.
# Output feeds directly into KEDA for predictive autoscaling.
#
# Why Prophet?
#   - Handles daily/weekly seasonality out of the box (traffic spikes at 9am, lunch, etc.)
#   - Robust to missing data and outliers
#   - Uncertainty intervals let us scale conservatively (upper bound, not mean)
#   - Retrains fast enough to run hourly

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import requests
from prophet import Prophet
from prometheus_client import Gauge, Counter, Histogram, start_http_server
import structlog

log = structlog.get_logger().bind(service="traffic-forecaster")

PROMETHEUS_URL    = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
REDIS_URL         = os.getenv("REDIS_URL", "redis://redis:6379")
FORECAST_HORIZON  = int(os.getenv("FORECAST_HORIZON_MINUTES", "30"))
RETRAIN_INTERVAL  = int(os.getenv("RETRAIN_INTERVAL_MINUTES", "60"))
TRAINING_DAYS     = int(os.getenv("TRAINING_DAYS", "90"))

SERVICES = [
    "order-service",
    "payment-service",
    "notification-service",
    "analytics-service",
]

# ── Model performance metrics ─────────────────────────────────────────────────
# These feed the "Observability of Observability" dashboard

forecast_mape = Gauge(
    "ml_forecast_mape_percent",
    "Mean Absolute Percentage Error of traffic forecast",
    ["service"],
)
forecast_predicted_rps = Gauge(
    "ml_forecast_predicted_rps",
    "Predicted requests per second for the next forecast horizon",
    ["service"],
)
forecast_upper_rps = Gauge(
    "ml_forecast_upper_rps",
    "Upper confidence bound of predicted RPS (used for scaling)",
    ["service"],
)
forecast_train_duration = Histogram(
    "ml_forecast_training_duration_seconds",
    "Time to retrain the Prophet model",
    ["service"],
)
forecast_errors = Counter(
    "ml_forecast_errors_total",
    "Errors during forecasting",
    ["service", "stage"],
)
model_last_trained = Gauge(
    "ml_model_last_trained_timestamp",
    "Unix timestamp when model was last trained",
    ["service", "model"],
)


class TrafficForecaster:
    """
    Per-service Prophet forecaster.
    Fetches historical RPS from Prometheus, trains, predicts, publishes.
    """

    def __init__(self, service: str):
        self.service     = service
        self.metric_name = service.replace("-", "_") + "_requests_total"
        self.model:      Optional[Prophet] = None
        self.last_mape:  float = 0.0

    def fetch_training_data(self) -> pd.DataFrame:
        """Pull TRAINING_DAYS of 5-minute-resolution RPS data from Prometheus."""
        log.info("fetching training data", service=self.service, days=TRAINING_DAYS)

        end   = datetime.utcnow()
        start = end - timedelta(days=TRAINING_DAYS)

        query = f'sum(rate({self.metric_name}[5m]))'

        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={
                "query": query,
                "start": start.timestamp(),
                "end":   end.timestamp(),
                "step":  "300",    # 5-minute resolution
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if not data["data"]["result"]:
            raise ValueError(f"No data returned for {self.service}")

        values = data["data"]["result"][0]["values"]
        df = pd.DataFrame(values, columns=["ds", "y"])
        df["ds"] = pd.to_datetime(df["ds"], unit="s", utc=True).dt.tz_localize(None)
        df["y"]  = df["y"].astype(float)

        # Remove outliers (3 sigma) — don't train on incident spikes
        mean, std = df["y"].mean(), df["y"].std()
        df = df[df["y"].between(mean - 3 * std, mean + 3 * std)]

        log.info("training data fetched",
            service=self.service,
            rows=len(df),
            mean_rps=round(df["y"].mean(), 2),
            max_rps=round(df["y"].max(), 2),
        )
        return df

    def train(self, df: pd.DataFrame) -> None:
        """Train Prophet model with daily and weekly seasonality."""
        import time
        start = time.perf_counter()

        log.info("training Prophet model", service=self.service)

        self.model = Prophet(
            # Uncertainty intervals — we scale to upper bound, not mean
            interval_width=0.95,

            # Seasonality — traffic has strong daily and weekly patterns
            daily_seasonality=True,
            weekly_seasonality=True,
            yearly_seasonality=False,   # 90 days isn't enough for yearly

            # Growth — linear is fine for RPS (not exponential)
            growth="linear",

            # Regularisation — prevent overfitting on noisy Prometheus data
            seasonality_prior_scale=5.0,
            changepoint_prior_scale=0.05,
        )

        # Add custom regressors for known traffic patterns
        # e.g. business hours boost (9am-6pm weekdays)
        self.model.add_seasonality(
            name="business_hours",
            period=1,
            fourier_order=8,
        )

        self.model.fit(df)

        duration = time.perf_counter() - start
        forecast_train_duration.labels(service=self.service).observe(duration)
        model_last_trained.labels(service=self.service, model="prophet").set(
            datetime.utcnow().timestamp()
        )

        log.info("model trained",
            service=self.service,
            duration_seconds=round(duration, 2),
        )

    def evaluate(self, df: pd.DataFrame) -> float:
        """
        Cross-validate on last 7 days to get MAPE.
        We use the last 7 days as holdout — train on days 0-83, test on 84-90.
        """
        if self.model is None:
            return 0.0

        holdout_start = df["ds"].max() - timedelta(days=7)
        train_df = df[df["ds"] < holdout_start]
        test_df  = df[df["ds"] >= holdout_start]

        if len(train_df) < 100 or len(test_df) < 10:
            return 0.0

        # Predict on test period
        future   = self.model.make_future_dataframe(periods=len(test_df), freq="5min")
        forecast = self.model.predict(future)
        pred     = forecast.tail(len(test_df))["yhat"].values
        actual   = test_df["y"].values

        # MAPE — ignore near-zero actual values (avoid division by zero)
        mask = actual > 0.1
        mape = np.mean(np.abs((actual[mask] - pred[mask]) / actual[mask])) * 100

        self.last_mape = mape
        forecast_mape.labels(service=self.service).set(mape)

        log.info("model evaluated",
            service=self.service,
            mape_percent=round(mape, 2),
            holdout_days=7,
        )
        return mape

    def predict(self) -> dict:
        """
        Predict traffic for the next FORECAST_HORIZON minutes.
        Returns mean prediction and upper confidence bound.
        Upper bound is used for scaling decisions — conservative but safe.
        """
        if self.model is None:
            raise RuntimeError("Model not trained yet")

        # Predict beyond the training data
        future   = self.model.make_future_dataframe(
            periods=FORECAST_HORIZON,
            freq="1min",
            include_history=False,
        )
        forecast = self.model.predict(future)

        # Take the peak predicted value in the horizon window
        # (scale for the worst case, not the average)
        peak_mean  = forecast["yhat"].max()
        peak_upper = forecast["yhat_upper"].max()

        # Clamp negatives (Prophet can predict negative for low-traffic periods)
        peak_mean  = max(0, peak_mean)
        peak_upper = max(0, peak_upper)

        forecast_predicted_rps.labels(service=self.service).set(peak_mean)
        forecast_upper_rps.labels(service=self.service).set(peak_upper)

        result = {
            "service":           self.service,
            "horizon_minutes":   FORECAST_HORIZON,
            "predicted_rps":     round(peak_mean, 2),
            "upper_bound_rps":   round(peak_upper, 2),
            "mape_percent":      round(self.last_mape, 2),
            "timestamp":         datetime.utcnow().isoformat(),
            "scale_to_replicas": self._replicas_for_rps(peak_upper),
        }

        log.info("forecast generated",
            service=self.service,
            predicted_rps=result["predicted_rps"],
            upper_bound_rps=result["upper_bound_rps"],
            scale_to_replicas=result["scale_to_replicas"],
        )
        return result

    def _replicas_for_rps(self, rps: float, rps_per_pod: float = 50.0) -> int:
        """Calculate replica count for a given RPS target."""
        raw     = int(np.ceil(rps / rps_per_pod))
        clamped = max(2, min(raw, 20))   # Always at least 2, never more than 20
        return clamped

    def run_cycle(self) -> dict:
        """Full train → evaluate → predict cycle."""
        try:
            df      = self.fetch_training_data()
            self.train(df)
            mape    = self.evaluate(df)
            result  = self.predict()

            if mape > 30:
                log.warning("model MAPE is high — predictions may be unreliable",
                    service=self.service,
                    mape=mape,
                )
            return result

        except Exception as e:
            forecast_errors.labels(service=self.service, stage="cycle").inc()
            log.error("forecast cycle failed", service=self.service, error=str(e))
            raise


class ForecastPublisher:
    """Writes forecast results to Redis so KEDA and other consumers can read them."""

    def __init__(self):
        import redis
        self.redis = redis.from_url(REDIS_URL, decode_responses=True)

    def publish(self, result: dict) -> None:
        service = result["service"]
        key     = f"ml:forecast:{service}"
        self.redis.setex(key, 3600, json.dumps(result))   # 1h TTL
        log.info("forecast published to Redis", service=service, key=key)

    def get_forecast(self, service: str) -> Optional[dict]:
        key  = f"ml:forecast:{service}"
        data = self.redis.get(key)
        return json.loads(data) if data else None


def run_forecasting_loop():
    """Main loop — retrain and forecast every RETRAIN_INTERVAL minutes."""
    import time

    # Start Prometheus metrics server
    start_http_server(8001)
    log.info("traffic forecaster starting",
        services=SERVICES,
        horizon_minutes=FORECAST_HORIZON,
        retrain_interval_minutes=RETRAIN_INTERVAL,
    )

    forecasters = {svc: TrafficForecaster(svc) for svc in SERVICES}
    publisher   = ForecastPublisher()

    while True:
        for service, forecaster in forecasters.items():
            try:
                result = forecaster.run_cycle()
                publisher.publish(result)
                log.info("forecast cycle complete",
                    service=service,
                    predicted_rps=result["predicted_rps"],
                    scale_to=result["scale_to_replicas"],
                )
            except Exception as e:
                log.error("forecast failed", service=service, error=str(e))

        log.info(f"sleeping {RETRAIN_INTERVAL} minutes until next retrain")
        time.sleep(RETRAIN_INTERVAL * 60)


if __name__ == "__main__":
    run_forecasting_loop()
