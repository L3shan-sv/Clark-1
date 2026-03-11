# ml/rl-agent/rl_agent.py
#
# Reinforcement Learning Remediation Agent
#
# Problem: Static if-then remediation rules don't learn from outcomes.
#          The same rule fires whether it worked last time or not.
#
# Solution: RL agent observes incident state → chooses action → measures outcome
#           → updates policy. Gets smarter every incident.
#
# Algorithm: Multi-Armed Bandit with Thompson Sampling
#   - Simpler than full RL (no environment model needed)
#   - Works well with sparse rewards (incidents are infrequent)
#   - Thompson Sampling handles exploration/exploitation naturally
#   - Each (incident_class, action) pair has a Beta distribution over success prob
#
# Shadow mode → Auto mode:
#   Starts in shadow mode — recommends but doesn't execute.
#   Graduates to auto mode after 50 observations with > 70% success rate.

import os
import json
import time
import numpy as np
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional
from enum import Enum

import redis
import structlog
from prometheus_client import Counter, Gauge, Histogram, start_http_server

log = structlog.get_logger().bind(service="rl-agent")

REDIS_URL          = os.getenv("REDIS_URL", "redis://redis:6379")
SHADOW_MODE        = os.getenv("SHADOW_MODE", "true").lower() == "true"
AUTO_GRADUATE_OBS  = int(os.getenv("AUTO_GRADUATE_OBSERVATIONS", "50"))
AUTO_GRADUATE_RATE = float(os.getenv("AUTO_GRADUATE_SUCCESS_RATE", "0.70"))

# ── Metrics ───────────────────────────────────────────────────────────────────

agent_actions_taken = Counter(
    "rl_agent_actions_total",
    "Total actions taken by RL agent",
    ["incident_class", "action", "mode"],
)
agent_action_success = Counter(
    "rl_agent_action_successes_total",
    "Successful remediations",
    ["incident_class", "action"],
)
agent_action_failure = Counter(
    "rl_agent_action_failures_total",
    "Failed remediations",
    ["incident_class", "action"],
)
agent_confidence = Gauge(
    "rl_agent_action_confidence",
    "Thompson sampling confidence for chosen action",
    ["incident_class", "action"],
)
agent_observations = Gauge(
    "rl_agent_observations_total",
    "Total observations per incident class",
    ["incident_class"],
)
time_to_remediation = Histogram(
    "rl_agent_time_to_remediation_seconds",
    "Time from incident detection to remediation complete",
    ["incident_class", "action"],
    buckets=[30, 60, 120, 300, 600, 1800],
)


# ── Incident Classes ──────────────────────────────────────────────────────────

class IncidentClass(str, Enum):
    POD_CRASH_LOOP       = "pod_crash_loop"
    HIGH_ERROR_RATE      = "high_error_rate"
    HIGH_LATENCY         = "high_latency"
    REDIS_MEMORY         = "redis_memory"
    KAFKA_LAG            = "kafka_lag"
    TRAFFIC_SPIKE        = "traffic_spike"
    NODE_PRESSURE        = "node_pressure"
    DEPLOYMENT_REGRESSION = "deployment_regression"


# ── Available Actions per Incident Class ──────────────────────────────────────

ACTIONS = {
    IncidentClass.POD_CRASH_LOOP: [
        "cordon_and_drain",
        "restart_pod",
        "increase_memory_limit",
        "rollback_deployment",
    ],
    IncidentClass.HIGH_ERROR_RATE: [
        "rollback_deployment",
        "scale_out",
        "circuit_break_upstream",
        "increase_timeout",
    ],
    IncidentClass.HIGH_LATENCY: [
        "scale_out",
        "increase_connection_pool",
        "rollback_deployment",
        "shed_low_priority_traffic",
    ],
    IncidentClass.REDIS_MEMORY: [
        "set_allkeys_lru",
        "increase_maxmemory",
        "flush_expired_keys",
        "scale_redis_vertical",
    ],
    IncidentClass.KAFKA_LAG: [
        "scale_out_consumers",
        "increase_consumer_threads",
        "reset_offsets_to_latest",
        "restart_consumer_group",
    ],
    IncidentClass.TRAFFIC_SPIKE: [
        "predictive_scale",
        "enable_rate_limiting",
        "activate_cache_warming",
        "shed_batch_traffic",
    ],
    IncidentClass.NODE_PRESSURE: [
        "cordon_and_drain",
        "evict_low_priority_pods",
        "trigger_cluster_autoscaler",
        "emergency_node_add",
    ],
    IncidentClass.DEPLOYMENT_REGRESSION: [
        "rollback_deployment",
        "canary_rollback_50pct",
        "feature_flag_disable",
        "scale_out_previous_revision",
    ],
}


# ── Thompson Sampling Bandit ───────────────────────────────────────────────────

@dataclass
class ArmState:
    """Beta distribution parameters for one (incident_class, action) pair."""
    action:   str
    alpha:    float = 1.0   # Successes + 1 (Beta prior)
    beta:     float = 1.0   # Failures + 1 (Beta prior)
    attempts: int   = 0

    @property
    def success_rate(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def confidence(self) -> float:
        """Inverse of variance — higher observations = higher confidence."""
        n = self.alpha + self.beta - 2   # subtract priors
        return min(1.0, n / 20)

    def sample(self) -> float:
        """Draw a sample from the Beta distribution."""
        return float(np.random.beta(self.alpha, self.beta))

    def update(self, success: bool) -> None:
        self.attempts += 1
        if success:
            self.alpha += 1
        else:
            self.beta  += 1


class ThompsonSamplingBandit:
    """
    Multi-armed bandit for one incident class.
    Each arm = one possible remediation action.
    """

    def __init__(self, incident_class: IncidentClass):
        self.incident_class = incident_class
        self.arms           = {
            action: ArmState(action=action)
            for action in ACTIONS.get(incident_class, [])
        }

    def choose_action(self) -> tuple[str, float]:
        """
        Thompson Sampling: draw from each arm's Beta distribution,
        pick the arm with highest sample. Balances exploration/exploitation.
        """
        if not self.arms:
            raise ValueError(f"No actions defined for {self.incident_class}")

        samples = {action: arm.sample() for action, arm in self.arms.items()}
        best    = max(samples, key=samples.__getitem__)
        return best, float(samples[best])

    def update(self, action: str, success: bool) -> None:
        if action in self.arms:
            self.arms[action].update(success)

    def best_action(self) -> tuple[str, float]:
        """Greedy best action (exploit only — use for reporting, not decisions)."""
        if not self.arms:
            return "", 0.0
        best = max(self.arms.values(), key=lambda a: a.success_rate)
        return best.action, best.success_rate

    def to_dict(self) -> dict:
        return {
            "incident_class": self.incident_class,
            "arms": {a: asdict(arm) for a, arm in self.arms.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ThompsonSamplingBandit":
        obj = cls(IncidentClass(data["incident_class"]))
        for action, arm_data in data.get("arms", {}).items():
            obj.arms[action] = ArmState(**arm_data)
        return obj


# ── RL Agent ──────────────────────────────────────────────────────────────────

class RLRemediationAgent:
    """
    The agent. Chooses actions, measures outcomes, updates policy.
    State is persisted to Redis so learning survives pod restarts.
    """

    REDIS_KEY = "ml:rl-agent:policy"

    def __init__(self):
        self.redis  = redis.from_url(REDIS_URL, decode_responses=True)
        self.bandits = self._load_policy()
        self.shadow_mode = SHADOW_MODE
        log.info("RL agent initialised",
            shadow_mode=self.shadow_mode,
            incident_classes=list(self.bandits.keys()),
        )

    def _load_policy(self) -> dict:
        """Load policy from Redis (or initialise fresh if none exists)."""
        raw = self.redis.get(self.REDIS_KEY)
        if raw:
            data = json.loads(raw)
            bandits = {}
            for cls_str, bandit_data in data.items():
                cls = IncidentClass(cls_str)
                bandits[cls] = ThompsonSamplingBandit.from_dict(bandit_data)
            log.info("policy loaded from Redis", incident_classes=len(bandits))
            return bandits

        # Fresh policy
        return {cls: ThompsonSamplingBandit(cls) for cls in IncidentClass}

    def _save_policy(self) -> None:
        """Persist policy to Redis."""
        data = {cls.value: bandit.to_dict() for cls, bandit in self.bandits.items()}
        self.redis.setex(self.REDIS_KEY, 60 * 60 * 24 * 90, json.dumps(data))

    def choose_action(self, incident_class: IncidentClass, context: dict) -> dict:
        """
        Choose the best action for an incident.
        In shadow mode: recommends but does not execute.
        In auto mode: returns action for Argo Workflow to execute.
        """
        bandit         = self.bandits[incident_class]
        action, sample = bandit.choose_action()
        greedy, rate   = bandit.best_action()

        agent_actions_taken.labels(
            incident_class=incident_class.value,
            action=action,
            mode="shadow" if self.shadow_mode else "auto",
        ).inc()

        agent_confidence.labels(
            incident_class=incident_class.value,
            action=action,
        ).set(sample)

        agent_observations.labels(
            incident_class=incident_class.value,
        ).set(bandit.arms[action].attempts)

        result = {
            "incident_class":   incident_class.value,
            "chosen_action":    action,
            "thompson_sample":  round(sample, 3),
            "greedy_action":    greedy,
            "greedy_rate":      round(rate, 3),
            "shadow_mode":      self.shadow_mode,
            "execute":          not self.shadow_mode,
            "timestamp":        datetime.utcnow().isoformat(),
            "arm_stats": {
                a: {
                    "success_rate": round(arm.success_rate, 3),
                    "attempts":     arm.attempts,
                    "confidence":   round(arm.confidence, 3),
                }
                for a, arm in bandit.arms.items()
            },
        }

        log.info("action chosen",
            incident_class=incident_class.value,
            action=action,
            shadow_mode=self.shadow_mode,
            greedy_action=greedy,
            greedy_rate=rate,
        )

        return result

    def record_outcome(
        self,
        incident_class: IncidentClass,
        action: str,
        success: bool,
        ttm_seconds: float,
    ) -> None:
        """
        Record the outcome of a remediation.
        Called by Argo Workflow exit handler after remediation completes.
        """
        bandit = self.bandits[incident_class]
        bandit.update(action, success)
        self._save_policy()

        if success:
            agent_action_success.labels(
                incident_class=incident_class.value,
                action=action,
            ).inc()
            time_to_remediation.labels(
                incident_class=incident_class.value,
                action=action,
            ).observe(ttm_seconds)
        else:
            agent_action_failure.labels(
                incident_class=incident_class.value,
                action=action,
            ).inc()

        log.info("outcome recorded",
            incident_class=incident_class.value,
            action=action,
            success=success,
            ttm_seconds=ttm_seconds,
            new_success_rate=round(bandit.arms[action].success_rate, 3),
        )

        # Check if agent should graduate from shadow to auto mode
        self._check_graduation(incident_class, action)

    def _check_graduation(self, incident_class: IncidentClass, action: str) -> None:
        """Graduate from shadow to auto mode when confidence is established."""
        if not self.shadow_mode:
            return

        bandit  = self.bandits[incident_class]
        total_obs = sum(arm.attempts for arm in bandit.arms.values())
        best_rate = max(arm.success_rate for arm in bandit.arms.values())

        if total_obs >= AUTO_GRADUATE_OBS and best_rate >= AUTO_GRADUATE_RATE:
            log.warning(
                "RL agent ready to graduate from shadow to auto mode",
                incident_class=incident_class.value,
                total_observations=total_obs,
                best_success_rate=round(best_rate, 3),
                recommendation="Set SHADOW_MODE=false to enable autonomous execution",
            )

    def policy_report(self) -> dict:
        """Generate a human-readable policy report for the dashboard."""
        report = {}
        for cls, bandit in self.bandits.items():
            greedy, rate = bandit.best_action()
            total_obs    = sum(arm.attempts for arm in bandit.arms.values())
            report[cls.value] = {
                "best_action":    greedy,
                "success_rate":   round(rate, 3),
                "total_attempts": total_obs,
                "shadow_mode":    self.shadow_mode,
                "arms": {
                    a: {
                        "success_rate": round(arm.success_rate, 3),
                        "attempts":     arm.attempts,
                    }
                    for a, arm in bandit.arms.items()
                },
            }
        return report


# ── FastAPI endpoint for Argo Workflows to call ───────────────────────────────

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="RL Remediation Agent", version="1.0.0")
agent = RLRemediationAgent()


class ActionRequest(BaseModel):
    incident_class: str
    context:        dict = {}


class OutcomeRequest(BaseModel):
    incident_class: str
    action:         str
    success:        bool
    ttm_seconds:    float


@app.post("/choose-action")
def choose_action(req: ActionRequest) -> dict:
    """Argo Workflow calls this to get the recommended action."""
    cls = IncidentClass(req.incident_class)
    return agent.choose_action(cls, req.context)


@app.post("/record-outcome")
def record_outcome(req: OutcomeRequest) -> dict:
    """Argo Workflow exit handler calls this with the result."""
    cls = IncidentClass(req.incident_class)
    agent.record_outcome(cls, req.action, req.success, req.ttm_seconds)
    return {"status": "recorded"}


@app.get("/policy")
def get_policy() -> dict:
    """Dashboard and reporting endpoint."""
    return agent.policy_report()


@app.get("/health/live")
def liveness():
    return {"status": "alive"}
