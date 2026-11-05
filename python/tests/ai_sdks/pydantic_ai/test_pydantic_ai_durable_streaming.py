# ABOUTME: Runs a harness agent on Pydantic AI's TemporalDurability capability end to end (time-
# skipping server, Pydantic AI's offline `test` model) and checks what reaches the turn stream:
# a model_interaction_started/ended bracket per model call, published from the model activity, with
# the call's usage on the ended event, reply deltas inside it, and the harness tool lifecycle.

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from pydantic_ai import Agent
from pydantic_ai.capabilities import ResolveModelId
from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
from pydantic_ai.exceptions import UserError
from pydantic_ai.models import Model, ModelResolutionContext
from pydantic_ai.models.instrumented import InstrumentedModel
from pydantic_ai.models.test import TestModel
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.pydantic_ai_harness import (
    HarnessDeps,
    TemporalAgentHarnessDurability,
    build_harness_toolset,
    harness_model,
)
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
from temporal_agent_harness.plugin import AgentHarnessPlugin


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city."""
    return f"It's 72°F and sunny in {city}."


def _weather_agent(
    model: Model | str, name: str, durability: TemporalAgentHarnessDurability | None = None
) -> Agent[HarnessDeps, str]:
    return Agent(
        model,
        name=name,
        deps_type=HarnessDeps,
        toolsets=[build_harness_toolset([get_weather], id="weather-tools")],
        capabilities=[durability or TemporalAgentHarnessDurability()],
    )


def _tenant_models(ctx: ModelResolutionContext[HarnessDeps], model_id: str) -> Model | None:
    """An author's resolver: builds a name infer_model doesn't know, defers on any other."""
    return TestModel() if model_id == "tenant-model" else None


# Pydantic AI's `test` model calls every tool once, then replies with their results as text. As a
# model-name string it is rebuilt inside the model activity through resolve_model_id; as an
# instance it is used there as-is, so it is passed wrapped in harness_model. `tenant-model` is a
# name only the author's resolver can build, so the run fails unless that resolver is consulted.
_NAMED_MODEL_AGENT = _weather_agent("test", "pydantic-ai-durable-streaming-named")
_INSTANCE_MODEL_AGENT = _weather_agent(
    harness_model(TestModel()), "pydantic-ai-durable-streaming-instance"
)
_RESOLVED_MODEL_AGENT = _weather_agent(
    "tenant-model",
    "pydantic-ai-durable-streaming-resolved",
    TemporalAgentHarnessDurability(model_resolver=_tenant_models),
)


class _WeatherAgentBase:
    def _start(self, config: AgentConfig, stream: WorkflowStream) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=stream,
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    async def _ask(self, durable_agent: Agent[HarnessDeps, str], text: str) -> TextReply:
        result = await durable_agent.run(text, deps=HarnessDeps(runner=self._runner))
        return TextReply(text=str(result.output))


@agent.defn(name="PydanticAINamedModelTestAgent")
class _NamedModelAgent(_WeatherAgentBase):
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._start(config, WorkflowStream())  # must be built in the @workflow.init method itself

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask about the weather."""
        return await self._ask(_NAMED_MODEL_AGENT, message.text)


@agent.defn(name="PydanticAIInstanceModelTestAgent")
class _InstanceModelAgent(_WeatherAgentBase):
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._start(config, WorkflowStream())  # must be built in the @workflow.init method itself

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask about the weather."""
        return await self._ask(_INSTANCE_MODEL_AGENT, message.text)


@agent.defn(name="PydanticAIResolvedModelTestAgent")
class _ResolvedModelAgent(_WeatherAgentBase):
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._start(config, WorkflowStream())  # must be built in the @workflow.init method itself

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask about the weather."""
        return await self._ask(_RESOLVED_MODEL_AGENT, message.text)


async def _one_turn(
    workflow_class: type[_WeatherAgentBase], durable_agent: Agent[HarnessDeps, str], text: str
) -> list[AgentEvent]:
    env = await WorkflowEnvironment.start_time_skipping(
        plugins=[PydanticAIPlugin(), AgentHarnessPlugin()]
    )
    try:
        task_queue = f"pydantic-ai-durable-streaming-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[workflow_class],
            plugins=[AgentPlugin(durable_agent)],
        ):
            handle = await env.client.start_workflow(
                workflow_class.run,  # type: ignore[attr-defined]  # added by @agent.defn
                AgentConfig(),
                id=f"pydantic-ai-durable-streaming-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            events: list[AgentEvent] = []
            stream = WorkflowStreamClient.create(env.client, handle.id)
            async with asyncio.timeout(30):
                await handle.execute_update(
                    SEND_AGENT_MESSAGE_UPDATE,
                    AgentMessage(type="ask", payload={"text": text}),
                    result_type=AgentMessageReply,
                )
                async for item in stream.subscribe(
                    topics=[TURN_EVENTS_TOPIC],
                    from_offset=0,
                    result_type=AgentEvent,
                    poll_cooldown=timedelta(milliseconds=10),
                ):
                    events.append(item.data)
                    if item.data.event.type == AgentEventType.TURN_END:
                        break
            return events
    finally:
        await env.shutdown()


@pytest.mark.parametrize(
    ("workflow_class", "durable_agent"),
    [
        pytest.param(_NamedModelAgent, _NAMED_MODEL_AGENT, id="model_name_string"),
        pytest.param(_InstanceModelAgent, _INSTANCE_MODEL_AGENT, id="harness_model_instance"),
        pytest.param(_ResolvedModelAgent, _RESOLVED_MODEL_AGENT, id="author_model_resolver"),
    ],
)
async def test_each_model_call_is_bracketed_from_the_activity_with_its_usage(
    workflow_class: type[_WeatherAgentBase], durable_agent: Agent[HarnessDeps, str]
):
    events = await _one_turn(workflow_class, durable_agent, "What's the weather in Paris?")
    types = [e.event.type for e in events]

    started = [e.event for e in events if e.event.type == AgentEventType.MODEL_INTERACTION_STARTED]
    ended = [e.event for e in events if e.event.type == AgentEventType.MODEL_INTERACTION_ENDED]
    # One call that asks for the tool, one that answers with its result.
    assert len(started) == len(ended) == 2
    assert all(s.model == "test" for s in started)
    assert [e.model for e in ended] == [s.model for s in started]
    assert all(end.usage is not None and end.usage.input_tokens for end in ended)
    # The test model counts only text as output, so only the reply call reports output tokens.
    assert ended[-1].usage is not None and ended[-1].usage.output_tokens

    # Each bracket closes before the next opens, and the reply streams inside the second one.
    first_end = types.index(AgentEventType.MODEL_INTERACTION_ENDED)
    second_start = types.index(AgentEventType.MODEL_INTERACTION_STARTED, first_end)
    second_end = types.index(AgentEventType.MODEL_INTERACTION_ENDED, second_start)
    deltas = [i for i, t in enumerate(types) if t == AgentEventType.REPLY_DELTA]
    assert deltas and all(second_start < i < second_end for i in deltas)

    # The tool call the first model call requested ran through the harness, in-workflow.
    requested = [e.event for e in events if e.event.type == AgentEventType.TOOL_REQUESTED]
    assert [r.tool_name for r in requested] == ["get_weather"]
    tool_ids = {
        e.event.tool_id
        for e in events
        if e.event.type in (AgentEventType.TOOL_START, AgentEventType.TOOL_END)
    }
    assert tool_ids == {requested[0].tool_id}


def test_an_unwrapped_model_instance_is_rejected_when_the_agent_is_built():
    with pytest.raises(UserError, match="harness_model"):
        _weather_agent(TestModel(), "pydantic-ai-unwrapped-instance")


@pytest.mark.parametrize("resolver_goes_first", [True, False], ids=["listed_before", "listed_after"])
def test_another_model_resolving_capability_is_rejected_when_the_agent_is_built(
    resolver_goes_first: bool,
):
    durability = TemporalAgentHarnessDurability()
    resolver = ResolveModelId(_tenant_models)
    with pytest.raises(UserError, match="ResolveModelId.*model_resolver"):
        Agent(
            "tenant-model",
            name="pydantic-ai-competing-resolver",
            deps_type=HarnessDeps,
            capabilities=[resolver, durability] if resolver_goes_first else [durability, resolver],
        )


def test_a_model_instance_wrapped_further_out_than_harness_model_is_accepted():
    _weather_agent(
        InstrumentedModel(harness_model(TestModel())), "pydantic-ai-instrumented-instance"
    )
