# ABOUTME: The full MCP tool path, end to end, on the configuration a real agent runs:
# the workflow SANDBOX, a streamed model call, and a genuine OpenAI Agents SDK MCP server
# behind stateless_mcp_server. Asserts the turn stream carries tool_requested -> tool_start
# -> tool_end under ONE tool_id, with no wiring in the agent (see _mcp_e2e_workflow.py).
#
# Why the sandbox matters, and why this test exists: the harness governs MCP calls from
# _convert_agent, which imports the harness lazily, while a workflow task is running. Under
# the sandbox that import returns a SECOND copy of the harness unless it is passed through
# -- a copy that cannot see the running agent -- and governance then no-ops without a word.
# The symptom is invisible to every unsandboxed test: tool_requested is published (it comes
# from the model stream) and nothing follows it, so the tool call vanishes from the UI.
#
# The model is faked, so no OPENAI_API_KEY is needed. Its raw Responses events are what the
# harness observer folds into tool_requested, which is what makes the id correlation real
# here rather than assumed.
#
# Run with: uv run pytest tests/ai_sdks/openai_agents/test_mcp_streaming_e2e.py -v

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from agents import Model
from agents.mcp import MCPServer
from mcp import types
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
)
from openai.types.responses.response import Response
from temporalio.client import Client
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents import ModelActivityParameters
from temporal_agent_harness.ai_sdks.openai_agents._mcp import StatelessMCPServerProvider
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

from ._mcp_e2e_workflow import (
    CALL_ID,
    MCP_SERVER_NAME,
    MCP_TOOL_NAME,
    McpStreamingAgent,
)

TOOL_OUTPUT = "mcp-result:hi"


class _ProbeMCPServer(MCPServer):  # type: ignore[misc]
    """A real in-process MCP server, reached through the stateless activity path."""

    @property
    def name(self) -> str:
        return MCP_SERVER_NAME

    async def connect(self) -> None: ...

    async def cleanup(self) -> None: ...

    async def list_tools(self, run_context: Any = None, agent: Any = None) -> list[Any]:
        return [
            types.Tool(
                name=MCP_TOOL_NAME,
                description="Look something up.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            )
        ]

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> types.CallToolResult:
        text = (arguments or {}).get("text")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"mcp-result:{text}")]
        )

    async def list_prompts(self) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=[])

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> types.GetPromptResult:
        raise NotImplementedError


def _response(output: list[Any]) -> Response:
    """A terminal Response complete enough to survive the activity round trip. Built
    field by field on purpose: a partial one fails revalidation and the SDK then reports
    'Model did not produce a final response'."""
    return Response(
        id="resp_1",
        created_at=0.0,
        model="gpt-5.1",
        object="response",
        output=output,
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
    )


class _StreamingToolCallModel(Model):
    """Turn one calls the MCP tool; turn two answers. Streams the raw Responses events
    the harness observer turns into tool_requested."""

    __test__ = False

    def __init__(self) -> None:
        self._turn = 0

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("this model only streams")

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self._turn += 1
        return self._tool_call_turn() if self._turn == 1 else self._reply_turn()

    async def _tool_call_turn(self) -> AsyncIterator[Any]:
        opening = ResponseFunctionToolCall.model_construct(
            type="function_call",
            id="fc_1",
            call_id=CALL_ID,
            name=MCP_TOOL_NAME,
            arguments="",
        )
        yield ResponseOutputItemAddedEvent.model_construct(
            type="response.output_item.added",
            item=opening,
            output_index=0,
            sequence_number=1,
        )
        yield ResponseFunctionCallArgumentsDeltaEvent.model_construct(
            type="response.function_call_arguments.delta",
            item_id="fc_1",
            delta='{"text":"hi"}',
            output_index=0,
            sequence_number=2,
        )
        yield ResponseFunctionCallArgumentsDoneEvent.model_construct(
            type="response.function_call_arguments.done",
            item_id="fc_1",
            name=MCP_TOOL_NAME,
            arguments='{"text":"hi"}',
            output_index=0,
            sequence_number=3,
        )
        done = ResponseFunctionToolCall.model_construct(
            type="function_call",
            id="fc_1",
            call_id=CALL_ID,
            name=MCP_TOOL_NAME,
            arguments='{"text":"hi"}',
            status="completed",
        )
        yield ResponseOutputItemDoneEvent.model_construct(
            type="response.output_item.done",
            item=done,
            output_index=0,
            sequence_number=4,
        )
        yield ResponseCompletedEvent.model_construct(
            type="response.completed", response=_response([done]), sequence_number=5
        )

    async def _reply_turn(self) -> AsyncIterator[Any]:
        yield ResponseTextDeltaEvent.model_construct(
            type="response.output_text.delta",
            delta="done",
            item_id="msg_1",
            output_index=0,
            content_index=0,
            logprobs=[],
            sequence_number=1,
        )
        message = ResponseOutputMessage.model_construct(
            id="msg_1",
            type="message",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText.model_construct(
                    type="output_text", text="done", annotations=[]
                )
            ],
        )
        yield ResponseOutputItemDoneEvent.model_construct(
            type="response.output_item.done",
            item=message,
            output_index=0,
            sequence_number=2,
        )
        yield ResponseCompletedEvent.model_construct(
            type="response.completed", response=_response([message]), sequence_number=3
        )


@pytest_asyncio.fixture
async def client_and_queue():
    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
            stream_to_provider=stream_to_provider,
        ),
        model_provider=TestModelProvider(_StreamingToolCallModel()),
        mcp_server_providers=[
            StatelessMCPServerProvider(MCP_SERVER_NAME, _ProbeMCPServer)
        ],
        observer_factory=harness_observer_factory,
    )
    env = await WorkflowEnvironment.start_time_skipping(plugins=[plugin])
    task_queue = f"mcp-streaming-{uuid.uuid4()}"
    # NOTE: the default sandboxed workflow runner, deliberately. See the module docstring.
    async with Worker(
        env.client, task_queue=task_queue, workflows=[McpStreamingAgent]
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _turn_events(client: Client, task_queue: str) -> list[AgentEvent]:
    handle = await client.start_workflow(
        McpStreamingAgent.run,
        AgentConfig(),
        id=f"mcp-streaming-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="ask", payload={"text": "hi"}, expected_turn=1),
        result_type=AgentMessageReply,
    )
    stream = WorkflowStreamClient.create(client, handle.id)
    events: list[AgentEvent] = []
    async with asyncio.timeout(60):
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


async def test_mcp_tool_call_brackets_under_the_sandbox(client_and_queue):
    client, task_queue = client_and_queue

    events = await _turn_events(client, task_queue)

    # One card's worth of lifecycle, all under the model's own call id. Before the
    # sandbox passthrough fix this was ["tool_requested"] alone: the call ran, but the
    # UI never saw it start or finish.
    assert [
        e.event.type for e in events if getattr(e.event, "tool_id", None) == CALL_ID
    ] == [
        AgentEventType.TOOL_REQUESTED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    end = next(e.event for e in events if e.event.type == AgentEventType.TOOL_END)
    assert end.tool_name == MCP_TOOL_NAME
    assert end.tool_output == TOOL_OUTPUT
    start = next(e.event for e in events if e.event.type == AgentEventType.TOOL_START)
    assert start.tool_input == {"text": "hi"}
    assert not [e for e in events if e.event.type == AgentEventType.ERROR]
