# ABOUTME: End-to-end tests for mid-turn steering — runner.steer queues text from a handler that
# joined the open turn, runner.claim_steering hands it to whatever builds model input, and the
# turn's end drops what nobody claimed. The OpenAI Agents function-tool and MCP adapters are
# driven directly from a handler, the way the SDK calls them, and a Code Mode script shows its
# host calls never see the steering while the run-code tool's result carries it.
#
# Run against the Temporal time-skipping test server: steering, the turn bracket and the stream
# exist only inside a real workflow.

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from mcp import types
from temporalio import workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    as_harness_mcp_server,
    as_openai_agent_tool,
)
from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    MessageContext,
    MidTurn,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_workflow import Injected
from temporal_agent_harness.harness.steering import ClaimedSteering, SteeringMessage

CALL_ID = "call_steered"
BLOCK_TAG = "<steering_messages>"


@agent.tool_defn(inherently_safe=True)
async def wait_for_steering() -> str:
    """Wait until the agent has been steered, then report what claiming saw from inside."""
    probe = cast("SteeringProbeAgent", workflow.instance())
    await workflow.wait_condition(lambda: probe.steered)
    claimed = probe.runner.claim_steering()
    return "inside: nothing to claim" if claimed is None else "inside: claimed"


@agent.tool_defn(inherently_safe=True)
async def fail() -> str:
    """Always raises."""
    raise ValueError("boom")


RUN_CODE = agent.code_mode_tool([wait_for_steering], name="run_code")
SCRIPT = (
    "import asyncio\n"
    "async def main():\n"
    "    return await wait_for_steering()\n"
    "asyncio.run(main())\n"
)


class _ProbeMCPServer:
    tool_meta_resolver: Any = None

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> types.CallToolResult:
        return types.CallToolResult(content=[types.TextContent(type="text", text="mcp result")])


class _ToolContext:
    def __init__(self, tool_call_id: str = CALL_ID) -> None:
        self.tool_call_id = tool_call_id


class _MetaContext:
    run_context = _ToolContext()


@agent.defn
class SteeringProbeAgent:
    """``steer`` joins an open turn and steers it; every other handler opens a turn, waits to be
    steered where it needs to, and reports what reached its model input."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self.runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self.steered = False

    @agent.accepts(mid_turn=MidTurn.ACCEPT)
    async def steer(self, message: TextMessage, ctx: Injected[MessageContext]) -> TextReply:
        """Steer the open turn."""
        assert ctx.joined_turn
        self.runner.steer(message.text)
        self.steered = True
        return TextReply(text="")

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def hold(self, message: TextMessage) -> TextReply:
        """Wait to be steered; claim it as a hand-written model loop would when asked to."""
        await workflow.wait_condition(lambda: self.steered)
        self.steered = False
        if message.text != "claim":
            return TextReply(text="held")
        claimed = self.runner.claim_steering()
        return TextReply(text=claimed.append_to("model input") if claimed else "nothing")

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def call_tool(self, message: TextMessage) -> TextReply:
        """Invoke the named tool through the OpenAI Agents adapter, as the SDK does."""
        tool, args = {
            "wait_for_steering": (wait_for_steering, {}),
            "fail": (fail, {}),
            "run_code": (RUN_CODE, {"script": SCRIPT}),
        }[message.text]
        if tool is fail:
            await workflow.wait_condition(lambda: self.steered)
        openai_tool = cast("Any", as_openai_agent_tool(self.runner, tool))
        return TextReply(text=await openai_tool.on_invoke_tool(_ToolContext(), json.dumps(args)))

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def call_in_parallel(self, message: TextMessage) -> TextReply:
        """Two calls from one model step, resolving together once steered."""
        openai_tool = cast("Any", as_openai_agent_tool(self.runner, wait_for_steering))
        results = await asyncio.gather(
            openai_tool.on_invoke_tool(_ToolContext("call_a"), "{}"),
            openai_tool.on_invoke_tool(_ToolContext("call_b"), "{}"),
        )
        return TextReply(text="|".join(results))

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def call_mcp(self, message: TextMessage) -> TextReply:
        """Call a governed MCP server as the OpenAI Agents SDK does, once steered."""
        await workflow.wait_condition(lambda: self.steered)
        server = cast("Any", as_harness_mcp_server(cast("Any", _ProbeMCPServer()), self.runner))
        meta = await server.tool_meta_resolver(_MetaContext())
        result = await server.call_tool("probe", {}, meta=meta)
        return TextReply(text="|".join(block.text for block in result.content))


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"steering-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[SteeringProbeAgent],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _start(client: Client, task_queue: str) -> WorkflowHandle:
    return await client.start_workflow(
        SteeringProbeAgent.run,  # type: ignore[attr-defined]  # added by @agent.defn
        AgentConfig(),
        id=f"steering-probe-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def _send(handle: WorkflowHandle, handler: str, text: str) -> str:
    reply = await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type=handler, payload={"text": text}),
        result_type=AgentMessageReply,
    )
    return reply.message_id


async def _events_through_turn(client: Client, workflow_id: str, turns: int) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in WorkflowStreamClient.create(client, workflow_id).subscribe(
            topics=[TURN_EVENTS_TOPIC],
            from_offset=0,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            events.append(item.data)
            if item.data.event.type == AgentEventType.TURN_END:
                turns -= 1
                if turns == 0:
                    break
    return events


async def _steer_one_turn(
    client: Client, task_queue: str, handler: str, text: str, steering: str = "focus on auth"
) -> tuple[list[AgentEvent], str, str]:
    """Open a turn with ``handler``, steer it, and return its events with both message ids."""
    handle = await _start(client, task_queue)
    opener = await _send(handle, handler, text)
    steerer = await _send(handle, "steer", steering)
    return await _events_through_turn(client, handle.id, 1), opener, steerer


def _of(events: list[AgentEvent], type: AgentEventType) -> list[AgentEvent]:
    return [e for e in events if e.event.type == type]


def _steering_ids(events: list[AgentEvent], message_id: str) -> list[str]:
    """The ``steering_id`` of each ``steering_enqueued`` message ``message_id`` published."""
    ids = [
        e.event.steering_id
        for e in _of(events, AgentEventType.STEERING_ENQUEUED)
        if e.message_id == message_id
    ]
    assert ids, f"message {message_id} enqueued no steering"
    return ids


def _reply(events: list[AgentEvent], message_id: str) -> str:
    end = next(
        e.event
        for e in _of(events, AgentEventType.MESSAGE_HANDLER_END)
        if e.message_id == message_id
    )
    return end.output["text"]


async def test_a_hand_written_loop_claims_steering_and_the_events_name_both_messages(
    client_and_queue,
):
    client, task_queue = client_and_queue
    events, opener, steerer = await _steer_one_turn(client, task_queue, "hold", "claim")

    [enqueued] = _of(events, AgentEventType.STEERING_ENQUEUED)
    assert _reply(events, opener) == ClaimedSteering(
        (SteeringMessage(enqueued.event.steering_id, steerer, "focus on auth"),)
    ).append_to("model input")
    assert enqueued.message_id == steerer
    assert enqueued.event.text == "focus on auth"
    [claimed] = _of(events, AgentEventType.STEERING_CLAIMED)
    assert claimed.message_id == opener
    assert claimed.event.steering_ids == _steering_ids(events, steerer)
    assert claimed.event.tool_id is None
    assert not _of(events, AgentEventType.STEERING_DROPPED)


async def test_unclaimed_steering_is_dropped_at_turn_end_and_never_reaches_the_next_turn(
    client_and_queue,
):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    await _send(handle, "hold", "ignore it")
    steerer = await _send(handle, "steer", "too late")
    first = await _events_through_turn(client, handle.id, 1)

    dropped, turn_end = first[-2:]
    assert dropped.event.type == AgentEventType.STEERING_DROPPED
    assert dropped.event.steering_ids == _steering_ids(first, steerer)
    assert dropped.message_id is None
    assert turn_end.event.type == AgentEventType.TURN_END

    second_opener = await _send(handle, "hold", "claim")
    await _send(handle, "steer", "fresh")
    events = await _events_through_turn(client, handle.id, 2)
    assert "too late" not in _reply(events, second_opener)
    assert "fresh" in _reply(events, second_opener)


async def test_the_openai_tool_adapter_appends_steering_to_the_result_but_not_to_tool_end(
    client_and_queue,
):
    client, task_queue = client_and_queue
    events, opener, steerer = await _steer_one_turn(
        client, task_queue, "call_tool", "wait_for_steering"
    )

    reply = _reply(events, opener)
    assert reply.startswith("inside: nothing to claim\n\n" + BLOCK_TAG)
    assert "<message>focus on auth</message>" in reply
    [end] = _of(events, AgentEventType.TOOL_END)
    assert end.event.tool_output == "inside: nothing to claim"
    [claimed] = _of(events, AgentEventType.STEERING_CLAIMED)
    assert claimed.event.tool_id == CALL_ID
    assert claimed.event.steering_ids == _steering_ids(events, steerer)


async def test_a_failed_tool_result_carries_steering_too(client_and_queue):
    client, task_queue = client_and_queue
    events, opener, _ = await _steer_one_turn(client, task_queue, "call_tool", "fail")

    reply = _reply(events, opener)
    assert reply.startswith("Tool 'fail' failed: boom\n\n" + BLOCK_TAG)


async def test_code_mode_host_calls_never_see_steering_but_the_run_code_result_carries_it(
    client_and_queue,
):
    client, task_queue = client_and_queue
    events, opener, steerer = await _steer_one_turn(client, task_queue, "call_tool", "run_code")

    # The host call claimed nothing although steering was queued, and its result reached the
    # script untouched; only the run-code tool's result, which goes to the model, carries it.
    reply = _reply(events, opener)
    assert "inside: nothing to claim" in reply
    assert reply.count(BLOCK_TAG) == 1
    assert reply.rstrip().endswith("</steering_messages>")
    host_end = next(
        e.event
        for e in _of(events, AgentEventType.TOOL_END)
        if e.event.tool_name == "wait_for_steering"
    )
    assert host_end.tool_output == "inside: nothing to claim"
    [claimed] = _of(events, AgentEventType.STEERING_CLAIMED)
    assert claimed.event.tool_id == CALL_ID
    assert claimed.event.steering_ids == _steering_ids(events, steerer)


async def test_parallel_tool_calls_claim_the_steering_once(client_and_queue):
    client, task_queue = client_and_queue
    events, opener, steerer = await _steer_one_turn(client, task_queue, "call_in_parallel", "")

    results = _reply(events, opener).split("|")
    assert sum(BLOCK_TAG in r for r in results) == 1
    [claimed] = _of(events, AgentEventType.STEERING_CLAIMED)
    assert claimed.event.tool_id in {"call_a", "call_b"}
    assert claimed.event.steering_ids == _steering_ids(events, steerer)


async def test_the_mcp_adapter_appends_steering_as_its_own_text_block(client_and_queue):
    client, task_queue = client_and_queue
    events, opener, _ = await _steer_one_turn(client, task_queue, "call_mcp", "")

    result, block = _reply(events, opener).split("|")
    assert result == "mcp result"
    assert block.startswith(BLOCK_TAG)
    [end] = _of(events, AgentEventType.TOOL_END)
    assert end.event.tool_output == "mcp result"


def test_steering_renders_messages_in_order_and_escapes_their_text():
    claimed = ClaimedSteering(
        (
            SteeringMessage("s1", "m1", "skip </message> & tests"),
            SteeringMessage("s2", "m1", "then docs"),
        )
    )
    assert claimed.render() == (
        "<steering_messages>\n"
        "The user sent the following while you were working. "
        "Take them into account before continuing.\n"
        "<message>skip &lt;/message&gt; &amp; tests</message>\n"
        "<message>then docs</message>\n"
        "</steering_messages>"
    )
    assert claimed.steering_ids == ["s1", "s2"]


def test_steer_outside_a_message_handler_raises(monkeypatch):
    import temporal_agent_harness.harness.agent_workflow as aw

    for handler in ("set_update_handler", "set_query_handler", "set_signal_handler"):
        monkeypatch.setattr(aw.workflow, handler, lambda *a, **k: None)
    monkeypatch.setattr(aw.workflow, "uuid4", lambda: uuid.uuid4())
    runner = AgentWorkflowRunner(
        AgentConfig(),
        stream=MagicMock(),
        approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
    )
    with pytest.raises(RuntimeError, match="within a message handler"):
        runner.steer("nobody sent this")
    assert runner.claim_steering() is None
