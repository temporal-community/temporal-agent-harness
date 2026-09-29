"""End-to-end durable execution coverage for OpenAI ``model='auto'``."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from agents import Model, ModelProvider
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents import ModelActivityParameters
from temporal_agent_harness.ai_sdks.openai_agents._temporal_openai_agents import (
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents.testing import (
    ResponseBuilders,
    TestModel,
)
from temporal_agent_harness.model_routing import router
from temporal_agent_harness.model_routing import activities as routing_activities
from temporal_agent_harness.model_routing.activities import route_model
from temporal_agent_harness.model_routing.classifier import Classification

from ._auto_routing_e2e_workflow import AutoRoutingWorkflow


class _RecordingModelProvider(ModelProvider):
    """Records the concrete OpenAI model selected before returning a fake model."""

    def __init__(self, model: Model) -> None:
        self.model = model
        self.model_names: list[str | None] = []

    def get_model(self, model_name: str | None) -> Model:
        self.model_names.append(model_name)
        return self.model


@pytest.mark.asyncio
async def test_auto_model_executes_one_recorded_route_then_uses_its_model(monkeypatch):
    """The real worker executes the router activity; OpenAI remains fake and offline."""
    classification = Classification(
        tier="complex",
        tier_confidence=0.96,
        stakes_score=0.0,
        stakes_confidence=0.99,
    )
    route_prompts: list[str] = []

    def route(_prompt: str, _config: object) -> router.RouteResult:
        route_prompts.append(_prompt)
        return router.RouteResult(
            model_id="gpt-5.6-sol",
            tier="complex",
            classification=classification,
            fallback_applied=False,
            stakes_override_applied=False,
        )

    monkeypatch.setattr(routing_activities, "route", route)
    provider = _RecordingModelProvider(
        TestModel.returning_responses([ResponseBuilders.output_message("done")])
    )
    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(start_to_close_timeout=timedelta(seconds=30)),
        model_provider=provider,
    )
    environment = await WorkflowEnvironment.start_time_skipping(plugins=[plugin])
    task_queue = f"auto-routing-{uuid.uuid4()}"
    try:
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[AutoRoutingWorkflow],
            activities=[route_model],
        ):
            result = await environment.client.execute_workflow(
                AutoRoutingWorkflow.run,
                id=f"auto-routing-{uuid.uuid4()}",
                task_queue=task_queue,
            )
    finally:
        await environment.shutdown()

    assert result == "done"
    assert route_prompts == ["Implement a multi-module refactor with a rollout plan."]
    assert provider.model_names == ["gpt-5.6-sol"]
