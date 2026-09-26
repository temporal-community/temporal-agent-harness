"""Workflow-side automatic model selection."""

from temporalio import workflow

from .models import (
    ROUTE_MODEL_ACTIVITY,
    ModelRoutingActivityOptions,
    ModelRoutingConfig,
    ModelRoutingDecision,
    ModelRoutingRequest,
)


async def resolve_model(
    configured_model: str,
    prompt: str,
    config: ModelRoutingConfig,
    activity_options: ModelRoutingActivityOptions,
) -> str:
    if configured_model != "auto":
        return configured_model

    decision = await workflow.execute_activity(
        ROUTE_MODEL_ACTIVITY,
        ModelRoutingRequest(prompt=prompt, config=config),
        result_type=ModelRoutingDecision,
        **activity_options.as_kwargs(),
    )
    return decision.model_id
