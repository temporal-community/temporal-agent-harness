# ABOUTME: Tests AgentConfig.opening_message — the message a caller with nobody at a keyboard
# (a cron dispatch, a console opening a session) hands the harness to run as the session's turn 1.
#
# The first test is the one that matters, and it is deliberately not a round-trip of our own
# making: it decodes the exact wire bytes a real start payload carries. A symmetric
# encode-then-decode cannot catch this bug, because dropping the field drops it from BOTH sides
# and the two blanks compare equal. That is precisely how the field went missing without a
# complaint — pydantic's default extra="ignore" discards an unknown key at the data-converter
# boundary, so the workflow started with a config that had quietly lost its only instruction, and
# every such session died replaying history event 5 (the memo upsert that never happened).
#
# The remaining tests cover what the field is FOR — the seeded turn is an ordinary turn, it is not
# re-run on the successor of a rollover, and an opening nobody can handle leaves a live session
# rather than bricking its first workflow task.
#
# Run with: uv run pytest tests/harness/test_opening_message.py -v

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio
from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    AGENT_STATUS_QUERY,
    INITIAL_USER_MESSAGE_MEMO,
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentMessage,
    AgentMessageReply,
    AgentResumeState,
    AgentStatus,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)

# Verbatim from the start payload of a live ``scheduled-daily-digest`` session (namespace
# agent-harness), which is what makes this a fixture and not a guess. Two details are load-bearing
# and both come from the wire rather than from taste: the config carries ONLY ``opening_message``,
# and the envelope has no ``expected_turn`` at all — an opening is authored before the session
# exists, so its author has no turn number to hold an opinion about.
LIVE_START_PAYLOAD = json.dumps(
    {
        "opening_message": {
            "type": "ask",
            "payload": {"text": "Prepare the daily review digest."},
        }
    }
).encode()


@workflow.defn
@agent.defn
class OpeningProbeAgent:
    """Records what it was asked, so a test can tell a seeded turn from no turn at all."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._seen: list[str] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the agent something."""
        self._seen.append(message.text)
        return TextReply(text=f"answered: {message.text}")

    @workflow.query
    def seen(self) -> list[str]:
        return self._seen


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"opening-message-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[OpeningProbeAgent],
        # Unsandboxed so the test module's own imports don't trip the workflow sandbox.
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _start(client: Client, task_queue: str, config: AgentConfig) -> WorkflowHandle:
    return await client.start_workflow(
        OpeningProbeAgent.run,
        config,
        id=f"opening-probe-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def _wait_for_seen(handle: WorkflowHandle, count: int) -> list[str]:
    for _ in range(200):
        seen = await handle.query("seen", result_type=list[str])
        if len(seen) >= count:
            return seen
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {count} handled messages")


@pytest.mark.asyncio
async def test_a_live_start_payload_keeps_its_opening_message_through_the_converter() -> None:
    """The bug, in one assertion: real wire bytes in, the instruction still there on the way out.

    Decoded through the SAME converter a worker uses, so an ``AgentConfig`` that stops carrying
    the field fails here rather than in production — where the loss is silent, and shows up one
    workflow task later as a session that will not replay.
    """
    payload = Payload(
        metadata={"encoding": b"json/plain"},
        data=LIVE_START_PAYLOAD,
    )

    config = (await pydantic_data_converter.decode([payload], [AgentConfig]))[0]

    opening = getattr(config, "opening_message", None)
    assert opening is not None, (
        "the start payload's opening_message was discarded by AgentConfig — the session it "
        "belongs to now starts with no instruction and dies replaying history event 5"
    )
    assert opening.type == "ask"
    assert opening.payload["text"] == "Prepare the daily review digest."


@pytest.mark.asyncio
async def test_a_config_built_in_process_keeps_its_opening_message_on_the_way_out() -> None:
    """The other half of the boundary: a caller CONSTRUCTING the config, whose keyword argument
    an ``extra="ignore"`` model would accept and then throw away just as quietly."""
    config = AgentConfig(
        opening_message=AgentMessage(type="ask", payload={"text": "open on this"})
    )

    encoded = (await pydantic_data_converter.encode([config]))[0]

    assert json.loads(encoded.data)["opening_message"]["payload"]["text"] == "open on this"


@pytest.mark.asyncio
async def test_an_opening_message_runs_as_turn_1_and_is_recorded_on_the_memo(
    client_and_queue,
) -> None:
    """The seeded turn is an ORDINARY turn — it dispatches to the handler, it takes turn 1, and it
    writes the session-list preview. That memo upsert is the command whose absence is what a
    dropped field costs: history records it at event 5, and a run that does not issue it cannot
    be replayed."""
    client, task_queue = client_and_queue
    handle = await _start(
        client,
        task_queue,
        AgentConfig(
            opening_message=AgentMessage(
                type="ask", payload={"text": "Prepare the daily review digest."}
            )
        ),
    )

    assert await _wait_for_seen(handle, 1) == ["Prepare the daily review digest."]

    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.current_turn == 1

    memo = await (await client.get_workflow_handle(handle.id).describe()).memo()
    assert (
        json.loads(memo[INITIAL_USER_MESSAGE_MEMO])["payload"]["text"]
        == "Prepare the daily review digest."
    )


@pytest.mark.asyncio
async def test_a_resumed_run_does_not_open_on_the_scenario_a_second_time(
    client_and_queue,
) -> None:
    """A successor run is handed the SAME config its predecessor got. Seeding there would replay
    the session's first turn on top of the conversation it just carried over — so the opening is
    held for a first run only, on the same reasoning as the memo flag beside it."""
    client, task_queue = client_and_queue
    handle = await _start(
        client,
        task_queue,
        AgentConfig(
            resume=AgentResumeState(turn_number=7),
            opening_message=AgentMessage(type="ask", payload={"text": "do not re-run me"}),
        ),
    )

    # No positive signal to wait for, so wait for the run to be settled and idle instead: the
    # status query answers only once the workflow has processed its first task.
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.current_turn == 7
    assert await handle.query("seen", result_type=list[str]) == []


@pytest.mark.asyncio
async def test_an_opening_nobody_can_handle_leaves_a_live_session(
    client_and_queue,
) -> None:
    """An opening is authored, so the way it goes wrong is a stale handler name — an authoring
    mistake. Raising would fail the workflow's FIRST task and retry it forever, and the reader
    would see a session that never starts. It stays up and waits to be spoken to instead."""
    client, task_queue = client_and_queue
    handle = await _start(
        client,
        task_queue,
        AgentConfig(
            opening_message=AgentMessage(type="no_such_handler", payload={"text": "hi"})
        ),
    )

    reply = await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="ask", payload={"text": "are you there"}, expected_turn=1),
        result_type=AgentMessageReply,
    )

    # Turn 1, not turn 2: the rejected opening consumed no turn number.
    assert reply.turn_number == 1
    assert await _wait_for_seen(handle, 1) == ["are you there"]
