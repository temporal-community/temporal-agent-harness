# ABOUTME: Regression test: a STREAMED model call with a structured output_type must return the
# structured final output. The model's stream runs in an activity whose collected events are
# replayed to the agents SDK in the workflow, which takes the final response from the terminal
# response.completed event; a structured reply carries a json_schema text format on that event.
#
# Run with: uv run pytest tests/ai_sdks/openai_agents/test_structured_streaming.py -v

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest_asyncio
from agents import Model
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextConfig,
    ResponseTextDeltaEvent,
)
from openai.types.responses.response import Response
from openai.types.responses.response_format_text_json_schema_config import (
    ResponseFormatTextJSONSchemaConfig,
)
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents import ModelActivityParameters
from temporal_agent_harness.ai_sdks.openai_agents._temporal_openai_agents import (
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents.testing import TestModelProvider
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
)
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from tests.ai_sdks.openai_agents._structured_stream_workflow import StructuredStreamingAgent

REPLY = '{"done": true, "note": "all set"}'


class _StructuredStreamingModel(Model):
    """Streams one structured reply, the way the Responses API does for a json_schema format:
    text deltas, the finished message, then response.completed carrying the format."""

    __test__ = False

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("this model only streams")

    async def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield ResponseTextDeltaEvent(
            type="response.output_text.delta",
            delta=REPLY,
            item_id="msg_1",
            output_index=0,
            content_index=0,
            logprobs=[],
            sequence_number=1,
        )
        message = ResponseOutputMessage(
            id="msg_1",
            type="message",
            role="assistant",
            status="completed",
            content=[ResponseOutputText(type="output_text", text=REPLY, annotations=[])],
        )
        yield ResponseOutputItemDoneEvent(
            type="response.output_item.done", item=message, output_index=0, sequence_number=2
        )
        yield ResponseCompletedEvent(
            type="response.completed",
            sequence_number=3,
            response=Response(
                id="resp_1",
                created_at=0.0,
                model="gpt-5.1",
                object="response",
                output=[message],
                parallel_tool_calls=False,
                tool_choice="auto",
                tools=[],
                text=ResponseTextConfig(
                    format=ResponseFormatTextJSONSchemaConfig(
                        type="json_schema",
                        name="final_output",
                        schema={"type": "object", "properties": {"done": {"type": "boolean"}}},
                        strict=True,
                    )
                ),
            ),
        )


@pytest_asyncio.fixture
async def client_and_queue():
    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
            stream_to_provider=stream_to_provider,
        ),
        model_provider=TestModelProvider(_StructuredStreamingModel()),
        observer_factory=harness_observer_factory,
    )
    env = await WorkflowEnvironment.start_time_skipping(plugins=[plugin, AgentHarnessPlugin()])
    task_queue = f"structured-streaming-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=task_queue, workflows=[StructuredStreamingAgent]):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def test_a_streamed_structured_reply_returns_its_output(client_and_queue):
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        StructuredStreamingAgent.run,
        AgentConfig(),
        id=f"structured-streaming-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    reply = await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="ask", payload={"text": "go"}),
        result_type=AgentMessageReply,
    )
    stream = WorkflowStreamClient.create(client, handle.id)
    async with asyncio.timeout(60):
        async for item in stream.subscribe(
            topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
        ):
            envelope: AgentEvent = item.data
            if envelope.message_id != reply.message_id:
                continue
            assert envelope.event.type != AgentEventType.MESSAGE_HANDLER_ERROR, envelope.event
            if envelope.event.type == AgentEventType.MESSAGE_HANDLER_END:
                assert envelope.event.output == {"done": True, "note": "all set"}
                return
    raise AssertionError("no reply")
