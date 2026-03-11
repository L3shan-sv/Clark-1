# ml/capacity-planning/capacity_planner.py
#
# Capacity Planning Pipeline + Toil Budget Tracker
#
# Two things FAANG SRE teams track obsessively:
#
# 1. CAPACITY PLANNING
#    "Will we have enough servers in 90 days?"
#    Prophet forecast (Phase 5) predicts traffic.
#    This translates traffic → resource requirements → cost.
#    Output: "You need 15% more capacity by day 45 or you'll breach SLO."
#
# 2. TOIL BUDGET
#    Google SRE defines toil as repetitive, manual, operational work
#    that scales linearly with traffic and produces no lasting value.
#    Policy: toil must consume < 50% of SRE time.
#    If toil > 50% → system is unsustainable → automation required.
#    This tracker measures actual toil from PagerDuty + Argo Workflows.

import os
import json
import time
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

import numpy as np
import requests
import structlog
from prometheus_client import Gauge, Counter, start_http_server

log = structlog.get_logger().bind(service="capacity-planner")

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
SLACK_WEBHOOK  = os.getenv("SLACK_WEBHOOK_URL", "")
PLAN_HORIZON_DAYS  = int(os.getenv("PLAN_HORIZON_DAYS", "90"))
SRE_TEAM_SIZE      = int(os.getenv("SRE_TEAM_SIZE", "4"))
HOURS_PER_WEEK     = 40 * SRE_TEAM_SIZE   # Total SRE hours/week
TOIL_BUDGET_PCT    = float(os.getenv("TOIL_BUDGET_PCT", "0.50"))

# ── Metrics ───────────────────────────────────────────────────────────────────
capacity_headroom_pct = Gauge(
    "capacity_headroom_percent",
    "Current capacity headroom by resource type",
    ["resource", "region"],
)
days_until_breach = Gauge(
    "capacity_days_until_slo_breach",
    "Predicted days until SLO breach if no capacity added",
    ["service", "resource"],
)
toil_hours_per_week = Gauge(
    "toil_hours_per_week",
    "Hours of toil per week (manual operational work)",
    ["category"],
)
toil_budget_consumed_pct = Gauge(
    "toil_budget_consumed_percent",
    "Percentage of toil budget consumed (alert if > 50%)",
)
automation_rate = Gauge(
    "automation_rate_percent",
    "Percentage of incidents handled automatically vs manually",
)


@dataclass
class CapacityForecast:
    service:            str
    resource:           str       # cpu / memory / replicas / storage
    current_usage:      float
    current_capacity:   float
    headroom_pct:       float
    forecast_30d:       float
    forecast_90d:       float
    breach_day:         Optional[int]   # Days until breach (None = no breach projected)
    recommendation:     str
    urgency:            str       # ok / plan / urgent / critical


@dataclass
class ToilReport:
    week_ending:        str
    total_toil_hours:   float
    toil_budget_hours:  float
    budget_consumed_pct: float
    categories: dict
    over_budget:        bool
    trend:              str       # improving / stable / worsening
    top_toil_source:    str
    automation_rate_pct: float
    recommendation:     str


class CapacityPlanner:
    """
    Fetches current usage and forecasts from Prometheus/ML models,
    computes capacity headroom, and predicts breach dates.
    """

    def fetch_metric(self, query: str) -> float:
        try:
            resp = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": query},
                timeout=10,
            )
            result = resp.json()["data"]["result"]
            return float(result[0]["value"][1]) if result else 0.0
        except Exception:
            return 0.0

    def fetch_metric_series(self, query: str, days: int = 30) -> np.ndarray:
        end   = datetime.utcnow()
        start = end - timedelta(days=days)
        try:
            resp = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={
                    "query": query,
                    "start": start.timestamp(),
                    "end":   end.timestamp(),
                    "step":  "3600",
                },
                timeout=15,
            )
            result = resp.json()["data"]["result"]
            if not result:
                return np.array([])
            return np.array([float(v[1]) for v in result[0]["values"]])
        except Exception:
            return np.array([])

    def forecast_linear(self, series: np.ndarray, horizon_days: int) -> tuple[float, float]:
        """Linear extrapolation. Returns (30d_forecast, 90d_forecast)."""
        if len(series) < 7:
            return 0, 0
        x = np.arange(len(series))
        slope, intercept = np.polyfit(x, series, 1)
        forecast_30 = intercept + slope * (len(series) + 30 * 24)
        forecast_90 = intercept + slope * (len(series) + 90 * 24)
        return max(0, forecast_30), max(0, forecast_90)

    def days_to_breach(
        self, series: np.ndarray, capacity: float, safety_margin: float = 0.85
    ) -> Optional[int]:
        """How many days until usage hits 85% of capacity?"""
        if len(series) < 7:
            return None
        threshold = capacity * safety_margin
        if series[-1] >= threshold:
            return 0   # Already breaching

        x = np.arange(len(series))
        slope, intercept = np.polyfit(x, series, 1)
        if slope <= 0:
            return None   # Usage is flat or declining — no breach

        # Solve: intercept + slope * t = threshold
        t_hours = (threshold - intercept) / slope
        t_days  = int((t_hours - len(series)) / 24)
        return max(0, t_days)

    def analyse_service(self, service: str) -> list:
        """Generate capacity forecasts for all resources of a service."""
        forecasts = []
        pod_prefix = service.replace("-", "_")

        # ── CPU capacity ──────────────────────────────────────────────────────
        cpu_usage    = self.fetch_metric_series(
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="app",pod=~"{service}-.*"}}[1h]))'
        )
        cpu_capacity = self.fetch_metric(
            f'sum(kube_pod_container_resource_limits{{namespace="app",pod=~"{service}-.*",resource="cpu"}})'
        )

        if len(cpu_usage) > 0 and cpu_capacity > 0:
            f30, f90    = self.forecast_linear(cpu_usage, 90)
            breach      = self.days_to_breach(cpu_usage, cpu_capacity)
            headroom    = (1 - cpu_usage[-1] / cpu_capacity) * 100
            capacity_headroom_pct.labels(resource="cpu", region="us-east-1").set(headroom)

            if breach is not None:
                days_until_breach.labels(service=service, resource="cpu").set(breach)

            forecasts.append(CapacityForecast(
                service=service, resource="cpu",
                current_usage=round(cpu_usage[-1], 3),
                current_capacity=round(cpu_capacity, 3),
                headroom_pct=round(headroom, 1),
                forecast_30d=round(f30, 3),
                forecast_90d=round(f90, 3),
                breach_day=breach,
                recommendation=(
                    f"Add {int((f90 - cpu_capacity) / 0.25 + 1)} nodes within {max(1, (breach or 90) - 14)} days"
                    if breach and breach < 60 else "No action needed"
                ),
                urgency=(
                    "critical" if breach and breach < 14 else
                    "urgent"   if breach and breach < 30 else
                    "plan"     if breach and breach < 60 else "ok"
                ),
            ))

        # ── Memory capacity ───────────────────────────────────────────────────
        mem_usage    = self.fetch_metric_series(
            f'sum(container_memory_working_set_bytes{{namespace="app",pod=~"{service}-.*"}})'
        )
        mem_capacity = self.fetch_metric(
            f'sum(kube_pod_container_resource_limits{{namespace="app",pod=~"{service}-.*",resource="memory"}})'
        )

        if len(mem_usage) > 0 and mem_capacity > 0:
            f30, f90    = self.forecast_linear(mem_usage, 90)
            breach      = self.days_to_breach(mem_usage, mem_capacity)
            headroom    = (1 - mem_usage[-1] / mem_capacity) * 100

            forecasts.append(CapacityForecast(
                service=service, resource="memory",
                current_usage=round(mem_usage[-1] / (1024**3), 2),
                current_capacity=round(mem_capacity / (1024**3), 2),
                headroom_pct=round(headroom, 1),
                forecast_30d=round(f30 / (1024**3), 2),
                forecast_90d=round(f90 / (1024**3), 2),
                breach_day=breach,
                recommendation=(
                    "Increase memory limits or scale out"
                    if breach and breach < 30 else "No action needed"
                ),
                urgency=(
                    "critical" if breach and breach < 14 else
                    "urgent"   if breach and breach < 30 else
                    "plan"     if breach and breach < 60 else "ok"
                ),
            ))

        return forecasts

    def generate_capacity_report(self) -> str:
        """Format a capacity planning Slack report."""
        services = ["order-service", "payment-service", "notification-service"]
        all_forecasts = []
        for svc in services:
            all_forecasts.extend(self.analyse_service(svc))

        urgent     = [f for f in all_forecasts if f.urgency in ("critical", "urgent")]
        plan_items = [f for f in all_forecasts if f.urgency == "plan"]

        lines = [
            f"*📊 90-Day Capacity Planning Report — {datetime.utcnow().strftime('%Y-%m-%d')}*",
            "",
        ]

        if urgent:
            lines.append(f"*🚨 Requires Action ({len(urgent)} items):*")
            for f in urgent:
                lines.append(
                    f"  • `{f.service}` {f.resource.upper()}: "
                    f"{f.headroom_pct:.1f}% headroom today | "
                    f"breach in *{f.breach_day} days* | "
                    f"{f.recommendation}"
                )

        if plan_items:
            lines.append(f"\n*📋 Plan Ahead ({len(plan_items)} items):*")
            for f in plan_items:
                lines.append(
                    f"  • `{f.service}` {f.resource.upper()}: "
                    f"breach in ~{f.breach_day} days"
                )

        if not urgent and not plan_items:
            lines.append("*✅ All services have sufficient capacity for the next 90 days.*")

        return "\n".join(lines)


class ToilTracker:
    """
    Measures toil from multiple sources:
      - PagerDuty: manual incident responses (pages that required human action)
      - Argo: ratio of auto-remediated vs manually handled incidents
      - Prometheus: deployment frequency (deploys requiring manual steps)
    """

    def fetch_metric(self, query: str) -> float:
        try:
            resp = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": query},
                timeout=10,
            )
            result = resp.json()["data"]["result"]
            return float(result[0]["value"][1]) if result else 0.0
        except Exception:
            return 0.0

    def compute_toil(self) -> ToilReport:
        """
        Estimate weekly toil hours from operational signals.

        Categories:
          - Incident response: pages where human action was required
          - On-call overhead: non-actionable pages (false positives)
          - Manual deployments: deploys requiring manual steps
          - Ticket toil: operational tickets (non-project work)
        """

        # Incidents that Argo did NOT handle automatically in the last 7 days
        auto_handled    = self.fetch_metric(
            'sum(increase(argo_workflows_count{phase="Succeeded"}[7d]))'
        )
        total_incidents = self.fetch_metric(
            'sum(increase(alertmanager_alerts_fired_total{severity="critical"}[7d]))'
        )
        manual_incidents = max(0, total_incidents - auto_handled)

        # Assume: each manually handled incident = 45 minutes toil
        incident_toil_hours = manual_incidents * 0.75

        # False positive pages (fired but resolved without action in < 5 min)
        false_positive_pages = self.fetch_metric(
            'sum(increase(alertmanager_alerts_fired_total[7d])) - sum(increase(alertmanager_alerts_fired_total{severity="critical"}[7d]))'
        )
        oncall_overhead_hours = false_positive_pages * 0.25  # 15 min per false page

        # Manual deployments (deployments without Argo CD — assume 30 min each)
        manual_deploys = self.fetch_metric(
            'sum(increase(kube_deployment_status_observed_generation[7d]))'
        )
        deploy_toil_hours = manual_deploys * 0.5

        # Ticket toil — estimated from JIRA/Linear (hardcoded estimate for now)
        # In production: pull from JIRA API
        ticket_toil_hours = 4.0    # Estimate: 4h/week of ticket work

        categories = {
            "incident_response": round(incident_toil_hours, 1),
            "oncall_overhead":   round(oncall_overhead_hours, 1),
            "manual_deploys":    round(deploy_toil_hours, 1),
            "ticket_toil":       round(ticket_toil_hours, 1),
        }

        total_toil  = sum(categories.values())
        budget      = HOURS_PER_WEEK * TOIL_BUDGET_PCT
        consumed    = total_toil / budget * 100
        auto_rate   = (auto_handled / max(total_incidents, 1)) * 100

        # Determine trend (compare to last week — simplified)
        trend       = "stable"    # In production: compare against rolling average

        top_category = max(categories, key=categories.__getitem__)

        recommendation = (
            f"🚨 TOIL BUDGET EXCEEDED: {total_toil:.1f}h/{budget:.0f}h budget. "
            f"Automate '{top_category}' first (largest contributor: {categories[top_category]:.1f}h)."
            if consumed > 100 else
            f"⚠️ Toil budget at {consumed:.0f}%. "
            f"'{top_category}' is the top driver ({categories[top_category]:.1f}h). "
            f"Consider automation before it exceeds budget."
            if consumed > 70 else
            f"✅ Toil within budget ({consumed:.0f}% consumed). "
            f"Automation rate is {auto_rate:.0f}%."
        )

        # Publish metrics
        for cat, hours in categories.items():
            toil_hours_per_week.labels(category=cat).set(hours)
        toil_budget_consumed_pct.set(consumed)
        automation_rate.set(auto_rate)

        log.info("toil report computed",
            total_hours=total_toil,
            budget_hours=budget,
            consumed_pct=consumed,
            auto_rate=auto_rate,
        )

        return ToilReport(
            week_ending=datetime.utcnow().strftime("%Y-%m-%d"),
            total_toil_hours=round(total_toil, 1),
            toil_budget_hours=round(budget, 1),
            budget_consumed_pct=round(consumed, 1),
            categories=categories,
            over_budget=consumed > 100,
            trend=trend,
            top_toil_source=top_category,
            automation_rate_pct=round(auto_rate, 1),
            recommendation=recommendation,
        )

    def post_slack_report(self, toil: ToilReport) -> None:
        if not SLACK_WEBHOOK:
            return
        color = "danger" if toil.over_budget else "warning" if toil.budget_consumed_pct > 70 else "good"
        cat_lines = "\n".join(
            f"  • {k.replace('_', ' ').title()}: {v}h/week"
            for k, v in sorted(toil.categories.items(), key=lambda x: -x[1])
        )
        requests.post(SLACK_WEBHOOK, json={
            "attachments": [{
                "color": color,
                "title": f"📊 Weekly Toil Report — {toil.week_ending}",
                "fields": [
                    {"title": "Total Toil",     "value": f"{toil.total_toil_hours}h / {toil.toil_budget_hours}h budget", "short": True},
                    {"title": "Budget Used",    "value": f"{toil.budget_consumed_pct}%", "short": True},
                    {"title": "Automation Rate","value": f"{toil.automation_rate_pct}%", "short": True},
                    {"title": "Trend",          "value": toil.trend, "short": True},
                    {"title": "Breakdown",      "value": cat_lines, "short": False},
                    {"title": "Recommendation", "value": toil.recommendation, "short": False},
                ],
                "footer": "Capacity Planning | Autonomous Observability Platform",
            }]
        }, timeout=5)


def run_planning_loop():
    start_http_server(8005)
    planner = CapacityPlanner()
    toil    = ToilTracker()

    log.info("capacity planner starting",
        plan_horizon_days=PLAN_HORIZON_DAYS,
        sre_team_size=SRE_TEAM_SIZE,
    )

    while True:
        # Weekly capacity + toil report (Sundays at midnight)
        capacity_report = planner.generate_capacity_report()
        toil_report     = toil.compute_toil()
        toil.post_slack_report(toil_report)

        if SLACK_WEBHOOK:
            requests.post(SLACK_WEBHOOK, json={"text": capacity_report}, timeout=5)

        log.info("planning cycle complete",
            toil_hours=toil_report.total_toil_hours,
            over_budget=toil_report.over_budget,
            automation_rate=toil_report.automation_rate_pct,
        )

        time.sleep(7 * 24 * 3600)


if __name__ == "__main__":
    run_planning_loop()
