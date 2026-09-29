"""Sandbox-safe workflow used by the automatic model selection test."""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from agents import Agent, Runner
    from temporal_agent_harness.model_routing import (
        ModelRoutingActivityOptions,
        OPENAI_MODEL_ROUTING_CONFIG,
        resolve_model,
    )


@workflow.defn
class AutoRoutingWorkflow:
    """One ordinary Agents SDK turn with a pre-resolved model."""

    @workflow.run
    async def run(self) -> str:
        model_id = await resolve_model(
            "auto",
            "Implement a multi-module refactor with a rollout plan.",
            OPENAI_MODEL_ROUTING_CONFIG,
            ModelRoutingActivityOptions(start_to_close_timeout=timedelta(seconds=30)),
        )
        result = await Runner.run(
            Agent(
                name="Auto routing test agent",
                instructions="Answer the request.",
                model=model_id,
            ),
            "Implement a multi-module refactor with a rollout plan.",
        )
        return str(result.final_output)
