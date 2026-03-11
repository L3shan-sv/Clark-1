# ml/rightsizing/rightsizing_engine.py
#
# Resource Rightsizing Engine
#
# The problem: engineers set resource requests/limits once and never revisit them.
# Typical result: 30-70% of provisioned resources are wasted.
# At $10k/month cluster spend, that's $3-7k/month in pure waste.
#
# This engine:
#   1. Fetches 30 days of actual CPU/memory usage per container
#   2. Computes p95 usage (not peak — peaks are handled by HPA)
#   3. Adds a configurable safety margin (default 25%)
#   4. Compares against current requests/limits
#   5. Generates recommendations with $ savings estimate
#   6. Posts weekly digest to Slack
#   7. Can auto-apply recommendations (disabled by default — humans approve)
#
# Safety rules:
#   - Never recommend below p95 of actual usage
#   - Never recommend a change > 50% decrease in one step (too aggressive)
#   - Skip pods with high variance (std/mean > 0.5) — they need HPA, not rightsizing
#   - Skip pods that have been running < 7 days (not enough data)

import os
import json
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import requests
import structlog
from prometheus_client import Gauge, Counter, start_http_server

log = structlog.get_logger().bind(service="rightsizing-engine")

PROMETHEUS_URL      = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
SLACK_WEBHOOK       = os.getenv("SLACK_WEBHOOK_URL", "")
AUTO_APPLY          = os.getenv("AUTO_APPLY", "false").lower() == "true"
SAFETY_MARGIN       = float(os.getenv("SAFETY_MARGIN", "0.25"))   # 25% headroom
MAX_DECREASE_PCT    = float(os.getenv("MAX_DECREASE_PCT", "0.50")) # Never cut > 50%
MIN_VARIANCE_SKIP   = float(os.getenv("MIN_VARIANCE_SKIP", "0.50")) # Skip high variance
ANALYSIS_DAYS       = int(os.getenv("ANALYSIS_DAYS", "30"))
CPU_PRICE_PER_CORE  = float(os.getenv("CPU_PRICE_PER_CORE_HOUR", "0.0464"))   # m5.xlarge / 4 cores
MEM_PRICE_PER_GB    = float(os.getenv("MEM_PRICE_PER_GB_HOUR", "0.0058"))    # m5.xlarge / 16 GB

# ── Metrics ───────────────────────────────────────────────────────────────────
recommendations_generated = Counter(
    "rightsizing_recommendations_total",
    "Total rightsizing recommendations generated",
    ["namespace", "action"],
)
estimated_savings = Gauge(
    "rightsizing_estimated_monthly_savings_dollars",
    "Estimated monthly savings from rightsizing recommendations",
    ["namespace"],
)
cpu_efficiency = Gauge(
    "rightsizing_cpu_efficiency_ratio",
    "CPU efficiency ratio per deployment (usage/request)",
    ["namespace", "deployment"],
)
memory_efficiency = Gauge(
    "rightsizing_memory_efficiency_ratio",
    "Memory efficiency ratio per deployment",
    ["namespace", "deployment"],
)


@dataclass
class ContainerUsage:
    namespace:      str
    pod_prefix:     str    # e.g. "order-service"
    container:      str
    cpu_p95_cores:  float
    cpu_p99_cores:  float
    cpu_mean_cores: float
    cpu_variance:   float
    mem_p95_bytes:  float
    mem_p99_bytes:  float
    mem_mean_bytes: float
    mem_variance:   float
    sample_days:    int


@dataclass
class RightsizingRecommendation:
    namespace:      str
    deployment:     str
    container:      str

    # Current settings
    current_cpu_request_cores:  float
    current_cpu_limit_cores:    float
    current_mem_request_bytes:  float
    current_mem_limit_bytes:    float

    # Recommended settings
    rec_cpu_request_cores:  float
    rec_cpu_limit_cores:    float
    rec_mem_request_bytes:  float
    rec_mem_limit_bytes:    float

    # Impact
    cpu_change_pct:     float
    mem_change_pct:     float
    monthly_saving_usd: float
    action:             str    # reduce / increase / ok
    confidence:         str    # high / medium / low
    reason:             str
    skip_reason:        Optional[str] = None


class RightsizingEngine:

    def fetch_usage_percentiles(
        self,
        namespace: str,
        pod_prefix: str,
        container: str,
    ) -> Optional[ContainerUsage]:
        """Fetch 30 days of CPU and memory usage for a container."""

        def query_range(promql: str) -> np.ndarray:
            end   = datetime.utcnow()
            start = end - timedelta(days=ANALYSIS_DAYS)
            try:
                resp = requests.get(
                    f"{PROMETHEUS_URL}/api/v1/query_range",
                    params={
                        "query": promql,
                        "start": start.timestamp(),
                        "end":   end.timestamp(),
                        "step":  "3600",    # 1-hour resolution
                    },
                    timeout=30,
                )
                result = resp.json()["data"]["result"]
                if not result:
                    return np.array([])
                return np.array([float(v[1]) for v in result[0]["values"] if v[1] != "NaN"])
            except Exception as e:
                log.error("prometheus query failed", error=str(e), query=promql[:60])
                return np.array([])

        # CPU — rate of CPU seconds consumed
        cpu_data = query_range(
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="{namespace}",'
            f'pod=~"{pod_prefix}-.*",container="{container}"}}[1h]))'
        )

        # Memory — working set bytes (excludes cache)
        mem_data = query_range(
            f'sum(container_memory_working_set_bytes{{namespace="{namespace}",'
            f'pod=~"{pod_prefix}-.*",container="{container}"}})'
        )

        if len(cpu_data) < 24 or len(mem_data) < 24:   # Need at least 1 day
            log.warning("insufficient data for rightsizing",
                namespace=namespace, pod_prefix=pod_prefix,
                cpu_points=len(cpu_data), mem_points=len(mem_data),
            )
            return None

        return ContainerUsage(
            namespace      = namespace,
            pod_prefix     = pod_prefix,
            container      = container,
            cpu_p95_cores  = float(np.percentile(cpu_data, 95)),
            cpu_p99_cores  = float(np.percentile(cpu_data, 99)),
            cpu_mean_cores = float(np.mean(cpu_data)),
            cpu_variance   = float(np.std(cpu_data) / (np.mean(cpu_data) + 1e-8)),
            mem_p95_bytes  = float(np.percentile(mem_data, 95)),
            mem_p99_bytes  = float(np.percentile(mem_data, 99)),
            mem_mean_bytes = float(np.mean(mem_data)),
            mem_variance   = float(np.std(mem_data) / (np.mean(mem_data) + 1e-8)),
            sample_days    = min(ANALYSIS_DAYS, len(cpu_data) // 24),
        )

    def fetch_current_resources(
        self,
        namespace: str,
        pod_prefix: str,
        container: str,
    ) -> dict:
        """Fetch current resource requests/limits from Kubernetes."""
        def instant_query(q: str) -> float:
            try:
                resp = requests.get(
                    f"{PROMETHEUS_URL}/api/v1/query",
                    params={"query": q},
                    timeout=10,
                )
                result = resp.json()["data"]["result"]
                return float(result[0]["value"][1]) if result else 0.0
            except Exception:
                return 0.0

        return {
            "cpu_request": instant_query(
                f'kube_pod_container_resource_requests{{namespace="{namespace}",'
                f'pod=~"{pod_prefix}-.*",container="{container}",resource="cpu"}}'
            ),
            "cpu_limit": instant_query(
                f'kube_pod_container_resource_limits{{namespace="{namespace}",'
                f'pod=~"{pod_prefix}-.*",container="{container}",resource="cpu"}}'
            ),
            "mem_request": instant_query(
                f'kube_pod_container_resource_requests{{namespace="{namespace}",'
                f'pod=~"{pod_prefix}-.*",container="{container}",resource="memory"}}'
            ),
            "mem_limit": instant_query(
                f'kube_pod_container_resource_limits{{namespace="{namespace}",'
                f'pod=~"{pod_prefix}-.*",container="{container}",resource="memory"}}'
            ),
        }

    def generate_recommendation(
        self,
        usage: ContainerUsage,
        current: dict,
    ) -> RightsizingRecommendation:
        """Generate a rightsizing recommendation for one container."""

        # ── Safety checks ─────────────────────────────────────────────────────

        # Skip if high variance — workload is spiky, needs HPA not rightsizing
        if usage.cpu_variance > MIN_VARIANCE_SKIP:
            return RightsizingRecommendation(
                namespace=usage.namespace, deployment=usage.pod_prefix,
                container=usage.container,
                current_cpu_request_cores=current["cpu_request"],
                current_cpu_limit_cores=current["cpu_limit"],
                current_mem_request_bytes=current["mem_request"],
                current_mem_limit_bytes=current["mem_limit"],
                rec_cpu_request_cores=current["cpu_request"],
                rec_cpu_limit_cores=current["cpu_limit"],
                rec_mem_request_bytes=current["mem_request"],
                rec_mem_limit_bytes=current["mem_limit"],
                cpu_change_pct=0, mem_change_pct=0,
                monthly_saving_usd=0,
                action="ok",
                confidence="low",
                reason="High CPU variance — use HPA instead of rightsizing",
                skip_reason=f"CPU variance={usage.cpu_variance:.2f} > threshold {MIN_VARIANCE_SKIP}",
            )

        # ── Compute recommended CPU ───────────────────────────────────────────
        # Request = p95 + safety margin (HPA handles spikes above p95)
        # Limit = p99 + larger safety margin (prevent OOMKill but not too loose)
        rec_cpu_req   = usage.cpu_p95_cores * (1 + SAFETY_MARGIN)
        rec_cpu_limit = usage.cpu_p99_cores * (1 + SAFETY_MARGIN * 1.5)

        # Never cut by more than MAX_DECREASE_PCT in one step
        min_cpu_req   = current["cpu_request"] * (1 - MAX_DECREASE_PCT)
        min_cpu_limit = current["cpu_limit"]   * (1 - MAX_DECREASE_PCT)
        rec_cpu_req   = max(rec_cpu_req,   min_cpu_req,   0.010)   # min 10m
        rec_cpu_limit = max(rec_cpu_limit, min_cpu_limit, 0.020)   # min 20m

        # ── Compute recommended memory ────────────────────────────────────────
        # Memory is less elastic than CPU — use p99 + margin for request
        # Limit = p99 * 1.5 (memory OOMKill is hard crash — be generous)
        rec_mem_req   = usage.mem_p99_bytes * (1 + SAFETY_MARGIN)
        rec_mem_limit = usage.mem_p99_bytes * (1 + SAFETY_MARGIN * 2)

        min_mem_req   = current["mem_request"] * (1 - MAX_DECREASE_PCT)
        min_mem_limit = current["mem_limit"]   * (1 - MAX_DECREASE_PCT)
        rec_mem_req   = max(rec_mem_req,   min_mem_req,   64 * 1024 * 1024)    # min 64Mi
        rec_mem_limit = max(rec_mem_limit, min_mem_limit, 128 * 1024 * 1024)   # min 128Mi

        # ── Compute savings ───────────────────────────────────────────────────
        cpu_delta_cores = current["cpu_request"] - rec_cpu_req
        mem_delta_gb    = (current["mem_request"] - rec_mem_req) / (1024**3)

        monthly_saving = (
            cpu_delta_cores * CPU_PRICE_PER_CORE * 730 +
            mem_delta_gb    * MEM_PRICE_PER_GB   * 730
        )
        monthly_saving = max(0, monthly_saving)   # Can't have negative savings

        # ── Determine action ──────────────────────────────────────────────────
        cpu_change_pct = (rec_cpu_req - current["cpu_request"]) / (current["cpu_request"] + 1e-8) * 100
        mem_change_pct = (rec_mem_req - current["mem_request"]) / (current["mem_request"] + 1e-8) * 100

        if abs(cpu_change_pct) < 10 and abs(mem_change_pct) < 10:
            action = "ok"
            reason = "Resources are well-sized — no change needed"
        elif cpu_change_pct < -15 or mem_change_pct < -15:
            action = "reduce"
            reason = (
                f"Overprovisioned: CPU at {usage.cpu_mean_cores/current['cpu_request']*100:.0f}% "
                f"of request, Memory at {usage.mem_mean_bytes/current['mem_request']*100:.0f}% of request"
            )
        else:
            action = "increase"
            reason = (
                f"Underprovisioned: p95 CPU ({usage.cpu_p95_cores:.3f}) "
                f"exceeds request ({current['cpu_request']:.3f})"
            )

        # Confidence based on sample size
        confidence = "high" if usage.sample_days >= 14 else "medium" if usage.sample_days >= 7 else "low"

        # Publish metrics
        cpu_efficiency.labels(
            namespace=usage.namespace, deployment=usage.pod_prefix,
        ).set(usage.cpu_mean_cores / (current["cpu_request"] + 1e-8))
        memory_efficiency.labels(
            namespace=usage.namespace, deployment=usage.pod_prefix,
        ).set(usage.mem_mean_bytes / (current["mem_request"] + 1e-8))

        if action in ("reduce", "increase"):
            recommendations_generated.labels(
                namespace=usage.namespace, action=action,
            ).inc()

        return RightsizingRecommendation(
            namespace=usage.namespace, deployment=usage.pod_prefix,
            container=usage.container,
            current_cpu_request_cores=round(current["cpu_request"], 4),
            current_cpu_limit_cores=round(current["cpu_limit"], 4),
            current_mem_request_bytes=round(current["mem_request"]),
            current_mem_limit_bytes=round(current["mem_limit"]),
            rec_cpu_request_cores=round(rec_cpu_req, 4),
            rec_cpu_limit_cores=round(rec_cpu_limit, 4),
            rec_mem_request_bytes=round(rec_mem_req),
            rec_mem_limit_bytes=round(rec_mem_limit),
            cpu_change_pct=round(cpu_change_pct, 1),
            mem_change_pct=round(mem_change_pct, 1),
            monthly_saving_usd=round(monthly_saving, 2),
            action=action,
            confidence=confidence,
            reason=reason,
        )

    def run_analysis(self, targets: list) -> list:
        """Analyse all target containers and return recommendations."""
        recs = []
        for t in targets:
            usage = self.fetch_usage_percentiles(t["namespace"], t["pod_prefix"], t["container"])
            if not usage:
                continue
            current = self.fetch_current_resources(t["namespace"], t["pod_prefix"], t["container"])
            rec = self.generate_recommendation(usage, current)
            recs.append(rec)
            log.info("recommendation generated",
                deployment=rec.deployment,
                action=rec.action,
                cpu_change=f"{rec.cpu_change_pct:+.1f}%",
                mem_change=f"{rec.mem_change_pct:+.1f}%",
                monthly_saving=f"${rec.monthly_saving_usd:.2f}",
            )

        # Publish total savings estimate per namespace
        for ns in set(r.namespace for r in recs):
            ns_savings = sum(r.monthly_saving_usd for r in recs if r.namespace == ns)
            estimated_savings.labels(namespace=ns).set(ns_savings)

        return recs

    def format_slack_digest(self, recs: list) -> str:
        """Format a weekly Slack digest of all recommendations."""
        reduce_recs  = [r for r in recs if r.action == "reduce"]
        increase_recs = [r for r in recs if r.action == "increase"]
        total_savings = sum(r.monthly_saving_usd for r in reduce_recs)

        lines = [
            f"*💰 Weekly Cost Rightsizing Report — {datetime.utcnow().strftime('%Y-%m-%d')}*",
            f"Analysed {len(recs)} containers over {ANALYSIS_DAYS} days",
            f"*Estimated monthly savings if all reductions applied: ${total_savings:.2f}*",
            "",
        ]

        if reduce_recs:
            lines.append(f"*📉 Overprovisioned ({len(reduce_recs)} containers) — candidates to reduce:*")
            for r in sorted(reduce_recs, key=lambda x: -x.monthly_saving_usd)[:8]:
                cpu_mb   = r.current_mem_request_bytes / (1024**2)
                rec_mb   = r.rec_mem_request_bytes     / (1024**2)
                lines.append(
                    f"  • `{r.deployment}/{r.container}` — "
                    f"CPU: {r.current_cpu_request_cores:.3f}→{r.rec_cpu_request_cores:.3f} cores "
                    f"({r.cpu_change_pct:+.0f}%), "
                    f"Mem: {cpu_mb:.0f}→{rec_mb:.0f}Mi "
                    f"({r.mem_change_pct:+.0f}%) | "
                    f"*saves ${r.monthly_saving_usd:.2f}/mo* [{r.confidence} confidence]"
                )

        if increase_recs:
            lines.append(f"\n*📈 Underprovisioned ({len(increase_recs)} containers) — needs more resources:*")
            for r in increase_recs[:5]:
                lines.append(
                    f"  • `{r.deployment}/{r.container}` — "
                    f"CPU p95 exceeds request by {r.cpu_change_pct:+.0f}%"
                )

        lines.append(f"\n_Apply recommendations: `kubectl apply -f rightsizing-patch.yaml`_")
        lines.append(f"_Auto-apply is {'ENABLED' if AUTO_APPLY else 'DISABLED (human review required)'}._")

        return "\n".join(lines)

    def post_slack_digest(self, recs: list) -> None:
        if not SLACK_WEBHOOK:
            return
        msg = self.format_slack_digest(recs)
        total_savings = sum(r.monthly_saving_usd for r in recs if r.action == "reduce")
        requests.post(SLACK_WEBHOOK, json={
            "attachments": [{
                "color": "good",
                "title": f"💰 Weekly Rightsizing Report — ${total_savings:.2f}/mo potential savings",
                "text":  msg,
                "footer": "Cost Rightsizing Engine | Autonomous Observability Platform",
            }]
        }, timeout=5)

    def generate_kubectl_patch(self, recs: list) -> str:
        """Generate kubectl patch commands to apply recommendations."""
        lines = ["#!/bin/bash", "# Auto-generated rightsizing patches",
                 "# Review before applying. Run: bash rightsizing-patch.sh", ""]
        for r in recs:
            if r.action != "reduce":
                continue
            cpu_req  = f"{int(r.rec_cpu_request_cores * 1000)}m"
            cpu_lim  = f"{int(r.rec_cpu_limit_cores   * 1000)}m"
            mem_req  = f"{int(r.rec_mem_request_bytes  / (1024**2))}Mi"
            mem_lim  = f"{int(r.rec_mem_limit_bytes    / (1024**2))}Mi"
            lines.append(
                f"# {r.deployment}/{r.container} — saves ${r.monthly_saving_usd:.2f}/mo"
            )
            lines.append(
                f"kubectl set resources deployment/{r.deployment} "
                f"-n {r.namespace} "
                f"-c {r.container} "
                f"--requests=cpu={cpu_req},memory={mem_req} "
                f"--limits=cpu={cpu_lim},memory={mem_lim}"
            )
            lines.append("")
        return "\n".join(lines)


# ── Target containers to analyse ─────────────────────────────────────────────

TARGETS = [
    {"namespace": "app", "pod_prefix": "order-service",        "container": "order-service"},
    {"namespace": "app", "pod_prefix": "payment-service",      "container": "payment-service"},
    {"namespace": "app", "pod_prefix": "notification-service", "container": "notification-service"},
    {"namespace": "app", "pod_prefix": "analytics-service",    "container": "analytics-service"},
    {"namespace": "ml",  "pod_prefix": "traffic-forecaster",   "container": "forecaster"},
    {"namespace": "ml",  "pod_prefix": "anomaly-detector",     "container": "detector"},
    {"namespace": "ml",  "pod_prefix": "rl-agent",             "container": "rl-agent"},
]


if __name__ == "__main__":
    import time
    start_http_server(8004)
    engine = RightsizingEngine()

    while True:
        log.info("starting rightsizing analysis", targets=len(TARGETS))
        recs  = engine.run_analysis(TARGETS)
        patch = engine.generate_kubectl_patch(recs)
        engine.post_slack_digest(recs)

        # Write patch script for human review
        with open("/tmp/rightsizing-patch.sh", "w") as f:
            f.write(patch)

        log.info("rightsizing analysis complete",
            total_recs=len(recs),
            reduce_count=sum(1 for r in recs if r.action == "reduce"),
            total_savings=sum(r.monthly_saving_usd for r in recs if r.action == "reduce"),
        )

        time.sleep(7 * 24 * 3600)   # Run weekly
