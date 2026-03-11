# ml/causal-inference/causal_engine.py
#
# Causal Inference Engine — the FAANG differentiator.
#
# When an anomaly fires, most systems say "something is wrong with order-service."
# This system says: "order-service error rate is elevated because payment-service
# p99 latency increased 3 minutes ago, which correlates 0.87 with upstream
# Redis connection pool exhaustion."
#
# How it works:
#   1. Maintain a service dependency graph (who calls whom)
#   2. On anomaly: fetch metrics for ALL nodes in the graph
#   3. Run Granger causality tests — does metric A predict metric B?
#   4. Walk the graph upstream from the anomalous service
#   5. Score each candidate root cause by: correlation × temporal precedence × graph distance
#   6. Return ranked root causes with confidence scores
#   7. Auto-generate RCA draft

import os
import json
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field
from statsmodels.tsa.stattools import grangercausalitytests

import structlog

log = structlog.get_logger().bind(service="causal-engine")

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
SLACK_WEBHOOK  = os.getenv("SLACK_WEBHOOK_URL", "")
GRAFANA_URL    = os.getenv("GRAFANA_URL", "http://grafana")


# ── Service Dependency Graph ──────────────────────────────────────────────────
# Defines the call graph. Each service lists its upstream dependencies.
# Update this when new services are added.

DEPENDENCY_GRAPH = {
    "order-service": {
        "upstream": ["payment-service", "redis", "kafka"],
        "metrics": {
            "error_rate":   "sum(rate(order_service_requests_total{status_code=~'5..'}[1m])) / sum(rate(order_service_requests_total[1m]))",
            "latency_p95":  "histogram_quantile(0.95, sum by (le) (rate(order_service_request_duration_seconds_bucket[1m])))",
            "request_rate": "sum(rate(order_service_requests_total[1m]))",
        },
    },
    "payment-service": {
        "upstream": ["redis", "kafka"],
        "metrics": {
            "error_rate":   "sum(rate(payment_service_requests_total{status_code=~'5..'}[1m])) / sum(rate(payment_service_requests_total[1m]))",
            "latency_p95":  "histogram_quantile(0.95, sum by (le) (rate(payment_service_request_duration_seconds_bucket[1m])))",
            "processing_time": "histogram_quantile(0.95, sum by (le) (rate(payment_processing_duration_seconds_bucket[1m])))",
        },
    },
    "notification-service": {
        "upstream": ["kafka", "redis"],
        "metrics": {
            "consumer_lag": "kafka_consumergroup_lag{consumergroup='notification-service'}",
            "error_rate":   "sum(rate(notification_service_requests_total{status_code=~'5..'}[1m])) / sum(rate(notification_service_requests_total[1m]))",
        },
    },
    "redis": {
        "upstream": [],
        "metrics": {
            "memory_ratio": "redis_memory_used_bytes / redis_memory_max_bytes",
            "hit_rate":     "rate(redis_keyspace_hits_total[1m]) / (rate(redis_keyspace_hits_total[1m]) + rate(redis_keyspace_misses_total[1m]))",
            "connected_clients": "redis_connected_clients",
            "eviction_rate": "rate(redis_evicted_keys_total[1m])",
        },
    },
    "kafka": {
        "upstream": [],
        "metrics": {
            "consumer_lag":   "max(kafka_consumergroup_lag)",
            "producer_rate":  "sum(rate(kafka_server_brokertopicmetrics_messagesin_total[1m]))",
            "broker_health":  "kafka_brokers",
        },
    },
}


@dataclass
class CausalCandidate:
    service:     str
    metric:      str
    correlation: float
    granger_pvalue: float
    time_lag_seconds: int
    graph_distance: int
    confidence:  float
    description: str


@dataclass
class RCAReport:
    incident_service:  str
    incident_metric:   str
    timestamp:         str
    root_cause:        Optional[CausalCandidate]
    all_candidates:    list = field(default_factory=list)
    timeline:          list = field(default_factory=list)
    rca_draft:         str = ""
    generated_in_ms:   float = 0.0


class CausalEngine:
    """
    Traverses the service dependency graph to find root cause
    of an anomaly in the target service.
    """

    def __init__(self):
        self.graph = DEPENDENCY_GRAPH

    def fetch_metric_history(self, query: str, minutes: int = 30) -> np.ndarray:
        """Fetch last N minutes of a metric at 1-minute resolution."""
        end   = datetime.utcnow()
        start = end - timedelta(minutes=minutes)
        try:
            resp = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={
                    "query": query,
                    "start": start.timestamp(),
                    "end":   end.timestamp(),
                    "step":  "60",
                },
                timeout=15,
            )
            result = resp.json()["data"]["result"]
            if result:
                return np.array([float(v[1]) for v in result[0]["values"]])
            return np.array([])
        except Exception as e:
            log.error("metric fetch failed", query=query[:50], error=str(e))
            return np.array([])

    def granger_test(
        self,
        cause: np.ndarray,
        effect: np.ndarray,
        max_lag: int = 5,
    ) -> tuple[float, int]:
        """
        Test if `cause` Granger-causes `effect`.
        Returns (p_value, best_lag).
        Lower p-value = stronger causal relationship.
        """
        if len(cause) < 20 or len(effect) < 20:
            return 1.0, 0

        min_len = min(len(cause), len(effect))
        df = pd.DataFrame({
            "effect": effect[-min_len:],
            "cause":  cause[-min_len:],
        }).dropna()

        if len(df) < 15:
            return 1.0, 0

        try:
            results = grangercausalitytests(df[["effect", "cause"]], maxlag=max_lag, verbose=False)
            # Find lag with lowest p-value
            best_lag = min(results, key=lambda l: results[l][0]["ssr_ftest"][1])
            p_value  = results[best_lag][0]["ssr_ftest"][1]
            return float(p_value), int(best_lag)
        except Exception:
            return 1.0, 0

    def cross_correlation(self, a: np.ndarray, b: np.ndarray) -> float:
        """Pearson correlation between two metric series."""
        if len(a) < 5 or len(b) < 5:
            return 0.0
        min_len = min(len(a), len(b))
        a, b = a[-min_len:], b[-min_len:]
        if a.std() == 0 or b.std() == 0:
            return 0.0
        return float(np.corrcoef(a, b)[0, 1])

    def bfs_upstream(self, service: str) -> list:
        """
        BFS traversal of dependency graph upstream from the anomalous service.
        Returns list of (service, distance) tuples.
        """
        visited  = set()
        queue    = [(service, 0)]
        upstream = []

        while queue:
            current, distance = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            if current != service:  # Don't include the affected service itself
                upstream.append((current, distance))

            for dep in self.graph.get(current, {}).get("upstream", []):
                if dep not in visited:
                    queue.append((dep, distance + 1))

        return upstream

    def analyse(self, incident_service: str, incident_metric: str) -> RCAReport:
        """
        Main entry point. Run causal analysis for an incident.
        Returns a ranked RCA report generated within ~2 minutes of incident open.
        """
        import time
        start_time = time.perf_counter()

        log.info("causal analysis started",
            service=incident_service,
            metric=incident_metric,
        )

        report = RCAReport(
            incident_service=incident_service,
            incident_metric=incident_metric,
            timestamp=datetime.utcnow().isoformat(),
        )

        # Fetch the anomalous metric's history
        incident_query = (
            self.graph
            .get(incident_service, {})
            .get("metrics", {})
            .get(incident_metric, "")
        )
        if not incident_query:
            log.warning("no metric query found", service=incident_service, metric=incident_metric)
            return report

        effect_data = self.fetch_metric_history(incident_query, minutes=30)
        if len(effect_data) < 10:
            log.warning("insufficient effect data", service=incident_service)
            return report

        # ── BFS upstream — check every dependency ────────────────────────────
        upstream_services = self.bfs_upstream(incident_service)
        candidates        = []
        timeline          = []

        for dep_service, graph_distance in upstream_services:
            dep_metrics = self.graph.get(dep_service, {}).get("metrics", {})

            for metric_name, query in dep_metrics.items():
                cause_data = self.fetch_metric_history(query, minutes=30)
                if len(cause_data) < 10:
                    continue

                # Granger causality test
                p_value, lag = self.granger_test(cause_data, effect_data)

                # Correlation
                corr = self.cross_correlation(cause_data, effect_data)

                # Skip if no signal
                if p_value > 0.15 and abs(corr) < 0.3:
                    continue

                # Confidence score:
                # - Lower p-value = more confident (Granger)
                # - Higher |correlation| = more confident
                # - Closer in graph = more likely (distance penalty)
                granger_conf  = max(0, 1 - p_value)
                corr_conf     = abs(corr)
                distance_penalty = 0.8 ** graph_distance

                confidence = (
                    0.5 * granger_conf +
                    0.3 * corr_conf +
                    0.2 * distance_penalty
                )

                candidate = CausalCandidate(
                    service          = dep_service,
                    metric           = metric_name,
                    correlation      = round(corr, 3),
                    granger_pvalue   = round(p_value, 4),
                    time_lag_seconds = lag * 60,
                    graph_distance   = graph_distance,
                    confidence       = round(confidence, 3),
                    description      = (
                        f"{dep_service}.{metric_name} shows "
                        f"{'positive' if corr > 0 else 'negative'} correlation "
                        f"({corr:.2f}) with {incident_service}.{incident_metric}, "
                        f"leading by {lag} minutes (p={p_value:.3f})."
                    ),
                )
                candidates.append(candidate)

                timeline.append({
                    "time":    f"T-{lag}min",
                    "service": dep_service,
                    "metric":  metric_name,
                    "signal":  f"{'↑' if corr > 0 else '↓'} {abs(corr):.0%} correlated",
                })

        # ── Rank candidates by confidence ─────────────────────────────────────
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        report.all_candidates = candidates
        report.root_cause     = candidates[0] if candidates else None
        report.timeline       = sorted(timeline, key=lambda t: t["time"])
        report.generated_in_ms = round((time.perf_counter() - start_time) * 1000, 1)

        # ── Generate RCA draft ────────────────────────────────────────────────
        report.rca_draft = self._generate_rca_draft(report)

        log.info("causal analysis complete",
            service=incident_service,
            candidates=len(candidates),
            root_cause=report.root_cause.service if report.root_cause else "unknown",
            confidence=report.root_cause.confidence if report.root_cause else 0,
            duration_ms=report.generated_in_ms,
        )

        return report

    def _generate_rca_draft(self, report: RCAReport) -> str:
        """Auto-generate a human-readable RCA draft."""
        rc = report.root_cause

        if not rc:
            return (
                f"# RCA Draft — {report.incident_service}\n\n"
                f"**Auto-generated at:** {report.timestamp}\n\n"
                f"**Root cause:** Could not identify a causal upstream factor. "
                f"Manual investigation required.\n\n"
                f"**Affected service:** {report.incident_service}\n"
                f"**Affected metric:** {report.incident_metric}\n"
            )

        return f"""# RCA Draft — {report.incident_service} (Auto-generated)

**Generated:** {report.timestamp} ({report.generated_in_ms}ms after incident open)
**Confidence:** {rc.confidence:.0%}

## Most Likely Root Cause
**{rc.service}** → `{rc.metric}`

{rc.description}

The degradation in `{rc.service}.{rc.metric}` appears to have preceded the
anomaly in `{report.incident_service}.{report.incident_metric}` by approximately
{rc.time_lag_seconds // 60} minute(s).

## Causal Chain
```
{rc.service}.{rc.metric}
  ↓ (leads by {rc.time_lag_seconds // 60}min, corr={rc.correlation})
{report.incident_service}.{report.incident_metric}  ← anomaly detected here
```

## All Candidate Causes (ranked by confidence)
{"".join([f"- **{c.service}.{c.metric}** — confidence {c.confidence:.0%}, corr={c.correlation}{chr(10)}" for c in report.all_candidates[:5]])}

## Recommended Investigation
1. Check `{rc.service}` dashboard: {GRAFANA_URL}/d/{rc.service}
2. Query in Prometheus: `{self.graph.get(rc.service, {}).get("metrics", {}).get(rc.metric, "")}`
3. Check {rc.service} logs in Loki: `{{app="{rc.service}"}} | json | level="error"`
4. If {rc.service} has had a recent deployment — consider rollback

---
*This RCA was auto-generated by the Causal Inference Engine.
Verify findings before treating as definitive.*
"""

    def post_to_slack(self, report: RCAReport) -> None:
        """Post RCA draft to Slack incident channel."""
        if not SLACK_WEBHOOK:
            return

        rc = report.root_cause
        requests.post(
            SLACK_WEBHOOK,
            json={
                "attachments": [{
                    "color": "warning",
                    "title": f"🔍 Auto-RCA: {report.incident_service}",
                    "fields": [
                        {
                            "title": "Most Likely Root Cause",
                            "value": f"{rc.service}.{rc.metric}" if rc else "Unknown",
                            "short": True,
                        },
                        {
                            "title": "Confidence",
                            "value": f"{rc.confidence:.0%}" if rc else "N/A",
                            "short": True,
                        },
                        {
                            "title": "Description",
                            "value": rc.description if rc else "No causal signal found.",
                            "short": False,
                        },
                        {
                            "title": "Generated in",
                            "value": f"{report.generated_in_ms}ms",
                            "short": True,
                        },
                    ],
                    "footer": "Causal Inference Engine | Autonomous Observability Platform",
                }]
            },
            timeout=5,
        )
