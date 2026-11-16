# ABOUTME: Runs the strands_hello example's own workflow end to end (time-skipping server, a scripted
# Strands model registered under the example's model name) and checks mid-turn steering: a message
# sent while the turn waits on ask_user is queued as steering and reaches the model with the next
# tool result.

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterable
from datetime import timedelta
from typing import Any

from strands.models import Model
from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment
from strands.types.streaming import StreamEvent
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from examples.strands_hello.workflow import (
    MODEL,
    SANDBOX,
    StrandsHelloAgentWorkflow,
    get_local_time,
)
from temporal_agent_harness.ai_sdks.strands_agents import StrandsPlugin
from temporal_agent_harness.ai_sdks.strands_agents_harness import harness_observer_factory
from temporal_agent_harness.harness.agent_protocol import (
    PROVIDE_CALLBACK_RESULT_UPDATE,
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    CallbackResult,
    TextReply,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin


class _AskThenEchoModel(Model):
    """Calls ask_user, then answers with exactly the tool result it was sent."""

    def __init__(self) -> None:
        self._calls = 0

    def update_config(self, **_model_config: Any) -> None:
        return None

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "scripted"}

    def structured_output(self, *_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError

    async def stream(self, messages: Any, *_args: Any, **_kwargs: Any) -> AsyncIterable[StreamEvent]:
        self._calls += 1
        yield {"messageStart": {"role": "assistant"}}
        if self._calls == 1:
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "tu-ask", "name": "ask_user"}}
                }
            }
            yield {
                "contentBlockDelta": {
                    "delta": {"toolUse": {"input": '{"question": "Which city?"}'}}
                }
            }
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        tool_result = next(
            block["toolResult"] for block in messages[-1]["content"] if "toolResult" in block
        )
        text = "".join(c.get("text", "") for c in tool_result["content"])
        yield {"contentBlockDelta": {"delta": {"text": text}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def _output_of(events: list[AgentEvent], message_id: str) -> TextReply:
    output = next(
        e.event.output
        for e in events
        if e.event.type == AgentEventType.MESSAGE_HANDLER_END and e.message_id == message_id
    )
    return TextReply.model_validate(output)


async def test_a_message_sent_mid_turn_steers_the_example_agent():
    plugin = StrandsPlugin(
        models={MODEL: _AskThenEchoModel},
        sandboxes={SANDBOX: lambda _context: NotASandboxLocalEnvironment()},
        observer_factory=harness_observer_factory,
    )
    env = await WorkflowEnvironment.start_time_skipping(
        plugins=[plugin, AgentHarnessPlugin(tools=[get_local_time])]
    )
    try:
        task_queue = f"strands-hello-{uuid.uuid4()}"
        async with Worker(env.client, task_queue=task_queue, workflows=[StrandsHelloAgentWorkflow]):
            handle = await env.client.start_workflow(
                StrandsHelloAgentWorkflow.run,  # type: ignore[attr-defined]  # added by @agent.defn
                AgentConfig(),
                id=f"strands-hello-{uuid.uuid4()}",
                task_queue=task_queue,
            )

            async def ask(text: str) -> str:
                reply = await handle.execute_update(
                    SEND_AGENT_MESSAGE_UPDATE,
                    AgentMessage(type="ask", payload={"text": text}),
                    result_type=AgentMessageReply,
                )
                return reply.message_id

            events: list[AgentEvent] = []
            steerer: str | None = None
            async with asyncio.timeout(30):
                asker = await ask("Plan a trip for me.")
                async for item in WorkflowStreamClient.create(env.client, handle.id).subscribe(
                    topics=[TURN_EVENTS_TOPIC],
                    from_offset=0,
                    result_type=AgentEvent,
                    poll_cooldown=timedelta(milliseconds=10),
                ):
                    events.append(item.data)
                    event = item.data.event
                    if event.type == AgentEventType.CALLBACK_REQUESTED:
                        # The turn is open, waiting on ask_user: this message steers it.
                        steerer = await ask("Only cities by the sea.")
                        await handle.execute_update(
                            PROVIDE_CALLBACK_RESULT_UPDATE,
                            CallbackResult(tool_id=event.tool_id, result="Lisbon"),
                        )
                    if event.type == AgentEventType.TURN_END:
                        break
    finally:
        await env.shutdown()

    assert steerer is not None
    assert _output_of(events, steerer).text == ""
    enqueued = next(e for e in events if e.event.type == AgentEventType.STEERING_ENQUEUED)
    assert enqueued.message_id == steerer
    claimed = next(e.event for e in events if e.event.type == AgentEventType.STEERING_CLAIMED)
    assert claimed.steering_ids == [enqueued.event.steering_id]
    assert claimed.tool_id == "tu-ask"

    tool_result, block = _output_of(events, asker).text.split("\n\n", 1)
    assert tool_result == "Lisbon"
    assert "<message>Only cities by the sea.</message>" in block
