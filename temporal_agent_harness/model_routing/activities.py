"""Worker-side activities for automatic model selection."""

from __future__ import annotations

import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .models import (
    ROUTE_MODEL_ACTIVITY,
    ModelRoutingConfig,
    ModelRoutingDecision,
    ModelRoutingRequest,
)
from .router import route


def _fallback(config: ModelRoutingConfig, reason: str) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        model_id=config.tier_to_model[config.fallback_tier],
        tier=config.fallback_tier,
        fallback_applied=True,
        stakes_override_applied=False,
        fallback_reason=reason,
    )


@activity.defn(name=ROUTE_MODEL_ACTIVITY)
async def route_model(request: ModelRoutingRequest) -> ModelRoutingDecision:
    if not request.prompt.strip():
        return _fallback(request.config, "no_routeable_text")

    try:
        decision = await asyncio.to_thread(route, request.prompt, request.config)
    except ModuleNotFoundError as error:
        if error.name == "typesafe_sdk":
            raise ApplicationError(
                "model='auto' requires the model-routing extra; install "
                "temporal-agent-harness[openai-agents,model-routing]",
                type="model_routing_not_installed",
                non_retryable=True,
            ) from error
        activity.logger.warning("Automatic model selector could not be imported")
        return _fallback(request.config, "router_unavailable")
    except Exception:
        activity.logger.warning("Automatic model selection failed; falling back")
        return _fallback(request.config, "router_unavailable")

    return ModelRoutingDecision(
        model_id=decision.model_id,
        tier=decision.tier,
        tier_confidence=decision.classification.tier_confidence,
        stakes_score=decision.classification.stakes_score,
        stakes_confidence=decision.classification.stakes_confidence,
        fallback_applied=decision.fallback_applied,
        stakes_override_applied=decision.stakes_override_applied,
    )
