# ABOUTME: Steers a harness agent running on Pydantic AI's TemporalDurability capability end to end
# (time-skipping server, a FunctionModel that answers with exactly what it was sent for the tool's
# result) and checks the steering reaches the model appended to the tool result, serialized the way
# Pydantic AI serializes a structured return, while tool_end keeps the tool's own output.

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from collections.abc import AsyncIterator
from typing import cast

from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from temporalio import workflow
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
    MidTurn,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

CALL_ID = "call_pick_city"


@agent.tool_defn(inherently_safe=True)
async def pick_city() -> dict[str, str]:
    """Pick a city, once the agent has been steered."""
    probe = cast("SteeredPydanticAgent", workflow.instance())
    await workflow.wait_condition(lambda: probe.steered)
    return {"city": "Lisbon"}


async def _echo_tool_result(
    messages: list[ModelMessage], info: AgentInfo
) -> AsyncIterator[str | DeltaToolCalls]:
    """Call pick_city once, then answer with exactly what the model was sent for its result."""
    last = messages[-1]
    parts = last.parts if isinstance(last, ModelRequest) else []
    returns = [p for p in parts if isinstance(p, ToolReturnPart)]
    if not returns:
        yield {0: DeltaToolCall(name="pick_city", json_args="{}", tool_call_id=CALL_ID)}
    else:
        yield returns[0].model_response_str()


_STEERED_AGENT = Agent(
    harness_model(FunctionModel(stream_function=_echo_tool_result)),
    name="pydantic-ai-steering",
    deps_type=HarnessDeps,
    toolsets=[build_harness_toolset([pick_city], id="city-tools")],
    capabilities=[TemporalAgentHarnessDurability()],
)


@agent.defn(name="SteeredPydanticAgent")
class SteeredPydanticAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self.runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self.steered = False

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def ask(self, message: TextMessage) -> TextReply:
        """Plan a trip."""
        result = await _STEERED_AGENT.run(message.text, deps=HarnessDeps(runner=self.runner))
        return TextReply(text=str(result.output))

    @agent.accepts(mid_turn=MidTurn.ACCEPT)
    async def steer(self, message: TextMessage) -> TextReply:
        """Steer the open turn."""
        self.runner.steer(message.text)
        self.steered = True
        return TextReply(text="")


async def test_steering_rides_the_serialized_tool_result_to_the_model():
    env = await WorkflowEnvironment.start_time_skipping(
        plugins=[PydanticAIPlugin(), AgentPlugin(_STEERED_AGENT)]
    )
    try:
        task_queue = f"pydantic-ai-steering-{uuid.uuid4()}"
        async with Worker(env.client, task_queue=task_queue, workflows=[SteeredPydanticAgent]):
            handle = await env.client.start_workflow(
                SteeredPydanticAgent.run,  # type: ignore[attr-defined]  # added by @agent.defn
                AgentConfig(),
                id=f"pydantic-ai-steering-{uuid.uuid4()}",
                task_queue=task_queue,
            )

            async def send(handler: str, text: str) -> str:
                reply = await handle.execute_update(
                    SEND_AGENT_MESSAGE_UPDATE,
                    AgentMessage(type=handler, payload={"text": text}),
                    result_type=AgentMessageReply,
                )
                return reply.message_id

            asker = await send("ask", "Plan my trip.")
            steerer = await send("steer", "Only cities by the sea.")
            events: list[AgentEvent] = []
            async with asyncio.timeout(30):
                async for item in WorkflowStreamClient.create(env.client, handle.id).subscribe(
                    topics=[TURN_EVENTS_TOPIC],
                    from_offset=0,
                    result_type=AgentEvent,
                    poll_cooldown=timedelta(milliseconds=10),
                ):
                    events.append(item.data)
                    if item.data.event.type == AgentEventType.TURN_END:
                        break
    finally:
        await env.shutdown()

    reply = next(
        e.event.output["text"]
        for e in events
        if e.event.type == AgentEventType.MESSAGE_HANDLER_END and e.message_id == asker
    )
    tool_result, block = reply.split("\n\n", 1)
    assert json.loads(tool_result) == {"city": "Lisbon"}
    assert block.startswith("<steering_messages>")
    assert "<message>Only cities by the sea.</message>" in block
    tool_end = next(e.event for e in events if e.event.type == AgentEventType.TOOL_END)
    assert "steering_messages" not in json.dumps(tool_end.tool_output)
    enqueued = next(e for e in events if e.event.type == AgentEventType.STEERING_ENQUEUED)
    assert enqueued.message_id == steerer
    claimed = next(e for e in events if e.event.type == AgentEventType.STEERING_CLAIMED)
    assert claimed.event.steering_ids == [enqueued.event.steering_id]
    assert claimed.event.tool_id == CALL_ID
    assert claimed.message_id == asker
