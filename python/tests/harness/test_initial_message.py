# ABOUTME: Tests for AgentConfig.initial_message — the runner admitting it as the agent's first
# message, a message sent with the start queuing behind it, a refused one failing the workflow,
# and a Temporal Schedule starting a session that runs one turn and closes.

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleSpec,
    WorkflowFailureError,
    WorkflowHandle,
)
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    AGENT_STATUS_QUERY,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentStatus,
    CloseReason,
    MessageDisposition,
    MidTurn,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)

HELLO = AgentMessage(type="ask", payload={"text": "hello"})


@agent.defn(name="StartedAgent")
class StartedAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def ask(self, message: TextMessage) -> TextReply:
        """Echo the question."""
        return TextReply(text=f"echo: {message.text}")


@pytest_asyncio.fixture
async def client_and_queue():
    # The dev server, since the time-skipping server has no Schedules.
    env = await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter)
    task_queue = f"initial-message-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[StartedAgent],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _start(client: Client, task_queue: str, config: AgentConfig) -> WorkflowHandle:
    return await client.start_workflow(
        "StartedAgent", config, id=f"StartedAgent-{uuid.uuid4()}", task_queue=task_queue
    )


async def _read_through_turn_end(
    client: Client, workflow_id: str, turn_number: int
) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    stream = WorkflowStreamClient.create(client, workflow_id).subscribe(
        topics=["turn_events"],
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    )
    async with asyncio.timeout(30):
        async for item in stream:
            events.append(item.data)
            event = item.data
            if event.event.type == AgentEventType.TURN_END and event.turn_number == turn_number:
                break
    return events


async def test_an_initial_message_runs_as_the_first_turn(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, AgentConfig(initial_message=HELLO))

    events = await _read_through_turn_end(client, handle.id, 1)

    assert [e.event.type for e in events] == [
        AgentEventType.MESSAGE_ACCEPTED,
        AgentEventType.TURN_STARTED,
        AgentEventType.MESSAGE_HANDLER_START,
        AgentEventType.MESSAGE_HANDLER_END,
        AgentEventType.TURN_END,
    ]
    accepted = events[0].event
    assert (accepted.handler, accepted.payload) == ("ask", {"text": "hello"})
    assert accepted.disposition == MessageDisposition.OPENED
    assert events[3].event.output == {"text": "echo: hello"}
    assert {e.turn_number for e in events} == {1}


async def test_a_message_sent_with_the_start_queues_behind_the_initial_message(client_and_queue):
    client, task_queue = client_and_queue
    workflow_id = f"StartedAgent-{uuid.uuid4()}"

    reply = await AgentClient(client, workflow_id).start_and_submit_message(
        "ask",
        {"text": "second"},
        workflow_name="StartedAgent",
        task_queue=task_queue,
        start_config=AgentConfig(initial_message=HELLO),
    )

    assert (reply.turn_number, reply.disposition) == (2, MessageDisposition.QUEUED)
    events = await _read_through_turn_end(client, workflow_id, 2)
    ended = [e.event.output for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END]
    assert ended == [{"text": "echo: hello"}, {"text": "echo: second"}]


@pytest.mark.parametrize(
    ("message", "error_type"),
    [
        (AgentMessage(type="no_such_handler", payload={}), "UnknownFunction"),
        (AgentMessage(type="ask", payload={"text": 1}), "MalformedMessage"),
    ],
)
async def test_an_initial_message_the_agent_refuses_fails_the_workflow(
    client_and_queue, message: AgentMessage, error_type: str
):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, AgentConfig(initial_message=message))

    with pytest.raises(WorkflowFailureError) as excinfo:
        await asyncio.wait_for(handle.result(), timeout=30)

    assert getattr(excinfo.value.cause, "type", None) == error_type


async def test_a_schedule_starts_a_session_that_runs_one_turn_and_closes(client_and_queue):
    client, task_queue = client_and_queue
    workflow_id = f"StartedAgent-{uuid.uuid4()}"
    config = AgentConfig(initial_message=HELLO, close_after_idle=timedelta(seconds=1))
    schedule = await client.create_schedule(
        f"schedule-{uuid.uuid4()}",
        Schedule(
            action=ScheduleActionStartWorkflow(
                "StartedAgent", config, id=workflow_id, task_queue=task_queue
            ),
            spec=ScheduleSpec(),
        ),
    )
    try:
        await schedule.trigger()
        async with asyncio.timeout(30):
            while not (actions := (await schedule.describe()).info.recent_actions):
                await asyncio.sleep(0.1)
    finally:
        await schedule.delete()

    # A Schedule adds the time of the run to the workflow id it was given.
    started = actions[0].action.workflow_id
    assert started.startswith(workflow_id)
    handle = client.get_workflow_handle(started)
    await asyncio.wait_for(handle.result(), timeout=30)

    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.close_reason == CloseReason.IDLE_TIMEOUT_EXPIRED
    assert status.current_turn == 1
