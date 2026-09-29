"""Workflow-safe models for automatic model selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator
from temporalio.common import Priority, RetryPolicy
from temporalio.workflow import ActivityCancellationType, VersioningIntent

from . import policy


ROUTE_MODEL_ACTIVITY = "temporal_agent_harness.route_model"


class ModelRoutingConfig(BaseModel):
    """A fixed set of models eligible for automatic selection."""

    model_config = ConfigDict(frozen=True)

    tier_to_model: dict[str, str]
    fallback_tier: str = policy.DEFAULT_TIER

    @model_validator(mode="after")
    def validate_tiers(self) -> "ModelRoutingConfig":
        missing_tiers = set(policy.TIER_ORDER) - self.tier_to_model.keys()
        if missing_tiers:
            missing = ", ".join(sorted(missing_tiers))
            raise ValueError(f"tier_to_model is missing required tiers: {missing}")
        if self.fallback_tier not in self.tier_to_model:
            raise ValueError("fallback_tier must be present in tier_to_model")
        return self


OPENAI_MODEL_ROUTING_CONFIG = ModelRoutingConfig(
    tier_to_model={
        "simple": "gpt-5.6-luna",
        "moderate": "gpt-5.6-terra",
        "complex": "gpt-5.6-sol",
        "frontier": "gpt-6-astra",
    }
)


class ModelRoutingRequest(BaseModel):
    prompt: str
    config: ModelRoutingConfig


class ModelRoutingDecision(BaseModel):
    model_id: str
    tier: str
    tier_confidence: float | None = None
    stakes_score: float | None = None
    stakes_confidence: float | None = None
    fallback_applied: bool
    stakes_override_applied: bool
    fallback_reason: str | None = None


@dataclass(frozen=True)
class ModelRoutingActivityOptions:
    task_queue: str | None = None
    schedule_to_close_timeout: Any = None
    schedule_to_start_timeout: Any = None
    start_to_close_timeout: Any = timedelta(seconds=60)
    heartbeat_timeout: Any = None
    retry_policy: RetryPolicy | None = None
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL
    versioning_intent: VersioningIntent | None = None
    priority: Priority = Priority.default

    def as_kwargs(self) -> dict[str, Any]:
        return {
            "task_queue": self.task_queue,
            "schedule_to_close_timeout": self.schedule_to_close_timeout,
            "schedule_to_start_timeout": self.schedule_to_start_timeout,
            "start_to_close_timeout": self.start_to_close_timeout,
            "heartbeat_timeout": self.heartbeat_timeout,
            "retry_policy": self.retry_policy,
            "cancellation_type": self.cancellation_type,
            "versioning_intent": self.versioning_intent,
            "priority": self.priority,
        }
