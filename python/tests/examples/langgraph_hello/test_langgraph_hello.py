# ABOUTME: Runs the langgraph_hello example's own workflow end to end (time-skipping server, a
# scripted chat model in place of ChatOpenAI) and checks mid-turn steering: a message sent while
# the turn waits on ask_user is queued as steering and reaches the model with the tool result.

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.chat_models import generate_from_stream
from langchain_core.messages import AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from examples.langgraph_hello import workflow as hello
from temporal_agent_harness.ai_sdks.langgraph import (
    LangGraphPlugin,
    harness_observer_factory,
    harness_tool_call,
)
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


def _reply(messages: list[BaseMessage]) -> list[AIMessageChunk]:
    """Calls ask_user, then answers with exactly the tool result it was sent."""
    if isinstance(messages[-1], ToolMessage):
        return [AIMessageChunk(content=str(messages[-1].content))]
    call = {
        "name": "ask_user",
        "args": json.dumps({"question": "Which city?"}),
        "id": "call-ask",
        "index": 0,
    }
    return [AIMessageChunk(content="", tool_call_chunks=[call])]


class _AskThenEchoModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        return generate_from_stream(ChatGenerationChunk(message=c) for c in _reply(messages))

    async def _astream(  # type: ignore[override]
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in _reply(messages):
            yield ChatGenerationChunk(message=chunk)


def _output_of(events: list[AgentEvent], message_id: str) -> TextReply:
    output = next(
        e.event.output
        for e in events
        if e.event.type == AgentEventType.MESSAGE_HANDLER_END and e.message_id == message_id
    )
    return TextReply.model_validate(output)


async def test_a_message_sent_mid_turn_steers_the_example_agent(monkeypatch: pytest.MonkeyPatch):
    # The model node runs in an activity in this process, where it builds its ChatOpenAI.
    clients: list[dict[str, Any]] = []

    def chat_openai(**kwargs: Any) -> _AskThenEchoModel:
        clients.append(kwargs)
        return _AskThenEchoModel()

    monkeypatch.setattr(hello, "ChatOpenAI", chat_openai)
    env = await WorkflowEnvironment.start_time_skipping(
        plugins=[
            LangGraphPlugin(
                graphs={hello.GRAPH: hello.build_graph()},
                awrap_tool_call=harness_tool_call,
                observer_factory=harness_observer_factory,
            ),
            AgentHarnessPlugin(tools=[hello.get_weather]),
        ]
    )
    try:
        task_queue = f"langgraph-hello-{uuid.uuid4()}"
        async with Worker(
            env.client, task_queue=task_queue, workflows=[hello.LangGraphHelloAgentWorkflow]
        ):
            handle = await env.client.start_workflow(
                hello.LangGraphHelloAgentWorkflow.run,  # type: ignore[attr-defined]  # added by @agent.defn
                AgentConfig(),
                id=f"langgraph-hello-{uuid.uuid4()}",
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

    # Temporal is the only retry layer: the model client must not retry inside the activity.
    assert clients and all(kwargs["max_retries"] == 0 for kwargs in clients)
    assert steerer is not None
    assert _output_of(events, steerer).text == ""
    enqueued = next(e for e in events if e.event.type == AgentEventType.STEERING_ENQUEUED)
    assert enqueued.message_id == steerer
    claimed = next(e.event for e in events if e.event.type == AgentEventType.STEERING_CLAIMED)
    assert claimed.steering_ids == [enqueued.event.steering_id]
    assert claimed.tool_id == "call-ask"

    tool_result, block = _output_of(events, asker).text.split("\n\n", 1)
    assert tool_result == "Lisbon"
    assert "<message>Only cities by the sea.</message>" in block
