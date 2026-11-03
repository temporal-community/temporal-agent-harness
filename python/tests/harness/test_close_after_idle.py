# ABOUTME: Tests for close_after_idle — the runner closing an agent that has sat idle for its
# configured time, the caller's AgentConfig overriding the agent's default, the agent_closed event
# and AgentStatus.close_reason, the AgentClosed rejection of messages sent after close, subagents
# never closing on idle, and a parent dropping a subagent something else closed. Also covers a
# failed subagent turn closing the parent's subagent bracket, which shares the parent's handling
# of a subagent's errors with the closed-subagent case.

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    AGENT_STATUS_QUERY,
    SEND_AGENT_MESSAGE_UPDATE,
    AgentClosed,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    AgentStatus,
    CloseReason,
    MidTurn,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.subagent_activities import SubagentActivities

IDLE_DEFAULT = timedelta(minutes=10)


class Work(BaseModel):
    """How long the handler keeps its turn open."""

    seconds: float


class ProbeData(BaseModel):
    """How long teardown keeps the closed workflow open."""

    teardown_seconds: float = 0


class Timeline(BaseModel):
    started_at: datetime
    last_turn_ended_at: datetime | None
    closed_at: datetime | None


@agent.defn(name="IdleProbeAgent")
class IdleProbeAgent:
    @agent.init
    def __init__(self, config: AgentConfig, data: ProbeData | None = None) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            close_after_idle_default=IDLE_DEFAULT,
        )
        self._data = data or ProbeData()
        self._timeline = Timeline(
            started_at=workflow.now(), last_turn_ended_at=None, closed_at=None
        )

    @agent.teardown
    async def teardown(self) -> None:
        self._timeline.closed_at = workflow.now()
        if self._data.teardown_seconds:
            await workflow.sleep(self._data.teardown_seconds)

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def work(self, message: Work) -> TextReply:
        """Hold the turn open for a while."""
        await workflow.sleep(message.seconds)
        self._timeline.last_turn_ended_at = workflow.now()
        return TextReply(text="done")

    @workflow.query
    def timeline(self) -> Timeline:
        return self._timeline


@agent.defn(name="IdleChildAgent")
class IdleChildAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            close_after_idle_default=timedelta(minutes=1),
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Echo the question, or fail when asked to."""
        if message.text == "fail":
            raise ValueError("child failed")
        return TextReply(text=message.text)


class StartChild(BaseModel):
    """Start an IdleChildAgent through the generated start tool."""

    task_queue: str


class AskChild(BaseModel):
    """Send one message to a started child through the generated send tool."""

    task_queue: str
    subagent: str
    text: str = "hello"


def _child_tools(task_queue: str):
    tools = agent.subagent_toolset(IdleChildAgent, key="child", task_queue=task_queue)
    return {t.__name__: t for t in tools}


@agent.defn(name="IdleParentAgent")
class IdleParentAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @agent.accepts
    async def start_child(self, message: StartChild) -> TextReply:
        """Start a child through the generated start tool."""
        start = _child_tools(message.task_queue)["start_child"]
        return TextReply(text=await self._runner.run_tool("start", start))

    @agent.accepts
    async def ask_child(self, message: AskChild) -> TextReply:
        """Ask a child something, reporting a failed send as text."""
        send = _child_tools(message.task_queue)["child_ask"]
        try:
            reply = await self._runner.run_tool(
                "ask", send, subagent=message.subagent, message={"text": message.text}
            )
        except Exception as e:  # noqa: BLE001 - the error text is what the test reads
            return TextReply(text=f"error: {e}")
        return TextReply(text=reply.text)


@pytest_asyncio.fixture
async def env_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"close-after-idle-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[IdleProbeAgent, IdleChildAgent, IdleParentAgent],
        activities=[SubagentActivities(env.client).run_subagent_turn],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env, task_queue
        finally:
            await env.shutdown()


async def _start(
    client: Client, task_queue: str, workflow_type: str, *args: BaseModel
) -> WorkflowHandle:
    return await client.start_workflow(
        workflow_type,
        args=list(args),
        id=f"{workflow_type}-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def _send(handle: WorkflowHandle, type: str, payload: dict) -> AgentMessageReply:
    return await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type=type, payload=payload),
        result_type=AgentMessageReply,
    )


async def _status(handle: WorkflowHandle) -> AgentStatus:
    return await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)


async def _read_until_closed(client: Client, workflow_id: str) -> list[AgentEvent]:
    """Tail the agent's stream until its agent_closed event (or the stream ends)."""
    events: list[AgentEvent] = []
    stream = WorkflowStreamClient.create(client, workflow_id).subscribe(
        topics=["turn_events"],
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    )
    async for item in stream:
        events.append(item.data)
        if item.data.event.type == AgentEventType.AGENT_CLOSED:
            break
    return events


def _assert_elapsed(start: datetime | None, end: datetime | None, expected: timedelta) -> None:
    """``end - start`` is ``expected``, give or take the workflow tasks between them."""
    assert start is not None and end is not None
    assert expected <= end - start < expected + timedelta(seconds=1)


async def _result(handle: WorkflowHandle) -> None:
    await asyncio.wait_for(handle.result(), timeout=30)


async def test_an_agent_with_no_messages_closes_once_its_idle_time_runs_out(env_and_queue):
    env, task_queue = env_and_queue
    handle = await _start(env.client, task_queue, "IdleProbeAgent", AgentConfig())
    reader = asyncio.create_task(_read_until_closed(env.client, handle.id))

    await _result(handle)

    timeline = await handle.query(IdleProbeAgent.timeline)
    _assert_elapsed(timeline.started_at, timeline.closed_at, IDLE_DEFAULT)
    assert (await _status(handle)).close_reason == CloseReason.IDLE_TIMEOUT_EXPIRED
    events = await asyncio.wait_for(reader, timeout=30)
    last = events[-1]
    assert last.event == AgentClosed(reason=CloseReason.IDLE_TIMEOUT_EXPIRED)
    assert (last.turn_id, last.turn_number, last.message_id) == ("", 0, None)


async def test_the_idle_clock_waits_out_a_long_turn_and_restarts_when_it_ends(env_and_queue):
    env, task_queue = env_and_queue
    handle = await _start(env.client, task_queue, "IdleProbeAgent", AgentConfig())
    # The turn runs well past the idle time; a running turn is never idle.
    long_turn = (IDLE_DEFAULT * 3).total_seconds()
    await _send(handle, "work", {"seconds": long_turn})

    await _result(handle)

    timeline = await handle.query(IdleProbeAgent.timeline)
    _assert_elapsed(timeline.last_turn_ended_at, timeline.closed_at, IDLE_DEFAULT)


async def test_a_callers_close_after_idle_overrides_the_agents_default(env_and_queue):
    env, task_queue = env_and_queue
    handle = await _start(
        env.client,
        task_queue,
        "IdleProbeAgent",
        AgentConfig(close_after_idle=timedelta(minutes=2)),
    )

    await _result(handle)

    timeline = await handle.query(IdleProbeAgent.timeline)
    _assert_elapsed(timeline.started_at, timeline.closed_at, timedelta(minutes=2))


async def test_a_zero_close_after_idle_switches_off_the_agents_default(env_and_queue):
    env, task_queue = env_and_queue
    handle = await _start(
        env.client, task_queue, "IdleProbeAgent", AgentConfig(close_after_idle=timedelta(0))
    )

    await env.sleep(IDLE_DEFAULT * 6)

    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
    assert (await _status(handle)).close_reason is None


async def test_a_closed_agent_rejects_new_messages_with_agent_closed(env_and_queue):
    env, task_queue = env_and_queue
    # Teardown holds the closed workflow open, so a send still reaches its validator.
    handle = await _start(
        env.client,
        task_queue,
        "IdleProbeAgent",
        AgentConfig(close_after_idle=timedelta(0)),
        ProbeData(teardown_seconds=3600),
    )
    reader = asyncio.create_task(_read_until_closed(env.client, handle.id))
    await handle.signal("close")
    events = await asyncio.wait_for(reader, timeout=30)
    assert events[-1].event == AgentClosed(reason=CloseReason.REQUESTED)

    with pytest.raises(WorkflowUpdateFailedError) as excinfo:
        await _send(handle, "work", {"seconds": 0})
    assert getattr(excinfo.value.cause, "type", None) == "AgentClosed"
    status = await _status(handle)
    assert status.close_reason == CloseReason.REQUESTED
    assert status.current_turn == 0 and status.pending_turns == []


async def _start_child(client: Client, task_queue: str) -> tuple[WorkflowHandle, str]:
    parent = await _start(
        client, task_queue, "IdleParentAgent", AgentConfig(close_after_idle=timedelta(0))
    )
    await _send(parent, "start_child", {"task_queue": task_queue})
    for _ in range(200):
        subagents = (await _status(parent)).subagents
        if subagents:
            return parent, subagents[0].subagent_id
        await asyncio.sleep(0.05)
    raise AssertionError("the parent never registered its child")


async def _child_handle(client: Client, parent: WorkflowHandle) -> WorkflowHandle:
    (info,) = (await _status(parent)).subagents
    return client.get_workflow_handle(info.workflow_id)


async def test_a_subagent_never_closes_on_idle(env_and_queue):
    env, task_queue = env_and_queue
    parent, _ = await _start_child(env.client, task_queue)
    child = await _child_handle(env.client, parent)

    # Well past the child's own one-minute default.
    await env.sleep(timedelta(hours=1))

    assert (await child.describe()).status == WorkflowExecutionStatus.RUNNING
    assert (await _status(child)).close_reason is None


async def _ask_child(
    parent: WorkflowHandle, task_queue: str, subagent: str, text: str = "hello"
) -> tuple[str, list[AgentEvent]]:
    """Ask the parent to message its child; return the parent's reply and its stream so far."""
    sent = await _send(
        parent, "ask_child", {"task_queue": task_queue, "subagent": subagent, "text": text}
    )
    events: list[AgentEvent] = []
    stream = WorkflowStreamClient.create(parent._client, parent.id).subscribe(
        topics=["turn_events"],
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    )
    async with asyncio.timeout(30):
        async for item in stream:
            events.append(item.data)
            event = item.data
            if event.event.type == AgentEventType.TURN_END and event.turn_number == sent.turn_number:
                break
    reply = next(
        e.event.output["text"]
        for e in reversed(events)
        if e.event.type == AgentEventType.MESSAGE_HANDLER_END
    )
    return reply, events


async def test_a_parent_drops_a_subagent_that_something_else_closed(env_and_queue):
    env, task_queue = env_and_queue
    parent, subagent = await _start_child(env.client, task_queue)
    child = await _child_handle(env.client, parent)

    await child.signal("close")
    await env.sleep(timedelta(seconds=1))
    assert (await child.describe()).status == WorkflowExecutionStatus.COMPLETED

    reply, events = await _ask_child(parent, task_queue, subagent)

    assert "has closed" in reply and "start_child" in reply
    assert (await _status(parent)).subagents == []
    stopped = [e.event for e in events if e.event.type == AgentEventType.SUBAGENT_STOPPED]
    assert [s.subagent_id for s in stopped] == [subagent]


async def test_a_failed_subagent_turn_closes_the_parents_subagent_bracket(env_and_queue):
    env, task_queue = env_and_queue
    parent, subagent = await _start_child(env.client, task_queue)

    reply, events = await _ask_child(parent, task_queue, subagent, text="fail")

    assert reply.startswith("error:")
    received = [
        e.event for e in events if e.event.type == AgentEventType.SUBAGENT_REPLY_RECEIVED
    ]
    assert [(r.subagent_id, r.outcome) for r in received] == [(subagent, "error")]
