# ABOUTME: Tests that a long-lived session rolls itself over into a fresh run without the
# person talking to it noticing — the conversation is still there, turn numbering carries on,
# stream offsets stay monotonic, and an attached stream client follows the handoff.
#
# These run against the Temporal time-skipping test server because a rollover is a Temporal
# decision, not a Python one: only a real server produces a successor run and rejects an update
# whose expected_turn does not match. The one thing the test server will not do on its own is
# decide that history is long enough, so ``Info.is_continue_as_new_suggested`` is patched to fire
# off ``get_current_history_length()``. That is the same quantity the real flag reports, and
# reading it keeps the decision replay-consistent — a threshold flipped by hand mid-run would
# make the workflow decide one thing live and another on replay.
#
# Run with: uv run pytest tests/harness/test_continue_as_new.py -v

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import aclosing, asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from temporalio import workflow
from temporalio.api.enums.v1 import EventType, ParentClosePolicy
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent, agent_workflow
from temporal_agent_harness.harness.agent_protocol import (
    AGENT_STATUS_QUERY,
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    AgentStatus,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.utils import large_payload

# History length past which the patched suggestion starts saying yes. Held in a dict so the
# patched method reads the live value; ``_NEVER`` is the resting state so a test that says
# nothing about rollover never gets one.
_NEVER = 10_000_000
_SUGGEST_AFTER = {"events": _NEVER}

# Any real session is past this by the end of its first turn, so a session that can roll over
# does so in the gap after that turn — which is the only gap it ever gets.
_AFTER_ONE_TURN = 5

# Comfortably over both Temporal's ~2 MB per-payload limit and the 1.5 MB threshold at which the
# harness's converter offloads to external storage.
_OVERSIZED_BYTES = 2_500_000


@pytest.fixture(autouse=True)
def suggestion_follows_history_length(monkeypatch):
    """Make the rollover suggestion a function of history length rather than server config."""
    monkeypatch.setattr(
        workflow.Info,
        "is_continue_as_new_suggested",
        lambda self: self.get_current_history_length() >= _SUGGEST_AFTER["events"],
    )
    _SUGGEST_AFTER["events"] = _NEVER
    yield
    _SUGGEST_AFTER["events"] = _NEVER


@pytest.fixture
def rollover_after_one_turn():
    """Ask for a rollover as soon as a session has completed a turn.

    Yields a callable that stops asking, for tests that want exactly one rollover and then a
    settled run to observe. Flipping it back is safe between runs but not within one: the run
    that has already decided to roll over must not replay into deciding otherwise.
    """
    _SUGGEST_AFTER["events"] = _AFTER_ONE_TURN
    return lambda: _SUGGEST_AFTER.update(events=_NEVER)


# ---------------------------------------------------------------------------
# Probe agents — one per veto, plus the one that actually rolls over
# ---------------------------------------------------------------------------


@workflow.defn(name="RolloverProbeAgent")
@agent.defn
class RolloverProbeAgent:
    """Remembers every message it has been sent and reads the whole list back."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            enable_message_queuing_default=True,
        )
        # Stands in for an SDK's private conversation state: the harness knows nothing about
        # it, which is the whole reason the hooks below have to exist.
        self._conversation: list[str] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Append this message to the conversation and read the whole thing back."""
        self._conversation.append(message.text)
        return TextReply(text=" ".join(self._conversation))

    @agent.snapshot
    def snapshot(self) -> dict[str, Any]:
        return {"conversation": self._conversation}

    @agent.restore
    def restore(self, state: dict[str, Any]) -> None:
        self._conversation = list(state["conversation"])


@workflow.defn(name="ForgetfulProbeAgent")
@agent.defn
class ForgetfulProbeAgent:
    """The same agent with no snapshot/restore pair, so it must never roll over."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._conversation: list[str] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Append this message to the conversation and read the whole thing back."""
        self._conversation.append(message.text)
        return TextReply(text=" ".join(self._conversation))


@workflow.defn(name="OwnStreamProbeAgent")
@agent.defn
class OwnStreamProbeAgent:
    """Declares the hooks but supplies its own stream, which the runner cannot hand over."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._conversation: list[str] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Append this message to the conversation and read the whole thing back."""
        self._conversation.append(message.text)
        return TextReply(text=" ".join(self._conversation))

    @agent.snapshot
    def snapshot(self) -> dict[str, Any]:
        return {"conversation": self._conversation}

    @agent.restore
    def restore(self, state: dict[str, Any]) -> None:
        self._conversation = list(state["conversation"])


@workflow.defn(name="SharedStreamProbeAgent")
@agent.defn
class SharedStreamProbeAgent:
    """Supplies its own stream and declares no pair — the documented way to trade one for the
    other, and the control that keeps the warning about the combination meaningful."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Say something back."""
        return TextReply(text=message.text)


@workflow.defn(name="LeakyProbeAgent")
@agent.defn
class LeakyProbeAgent:
    """Snapshots an SDK object rather than plain data — the mistake nothing else catches."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._conversation: list[TextReply] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Append this message to the conversation and read the whole thing back."""
        # Yields to the server once, as every real turn does by awaiting an activity. Without it
        # the update, the turn, and the refused rollover all land in one workflow task, and the
        # task failing means the update that accepted the message never completes either.
        await asyncio.sleep(0.001)
        self._conversation.append(TextReply(text=message.text))
        return TextReply(text=" ".join(reply.text for reply in self._conversation))

    @agent.snapshot
    def snapshot(self) -> dict[str, Any]:
        # The forgotten ``.model_dump()``. A naive round trip in one process hands these back
        # unchanged, and the pydantic converter encodes them without complaint — so nothing but
        # the harness's own check stands between this and a successor holding dicts.
        return {"conversation": self._conversation}

    @agent.restore
    def restore(self, state: dict[str, Any]) -> None:
        self._conversation = list(state["conversation"])


@workflow.defn(name="OversizedProbeAgent")
@agent.defn
class OversizedProbeAgent:
    """Carries a conversation larger than Temporal will accept as a single payload."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._conversation = ""

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Grow the conversation past the payload limit and report its size, not its content."""
        self._conversation += message.text * _OVERSIZED_BYTES
        return TextReply(text=str(len(self._conversation)))

    @agent.snapshot
    def snapshot(self) -> dict[str, Any]:
        return {"conversation": self._conversation}

    @agent.restore
    def restore(self, state: dict[str, Any]) -> None:
        self._conversation = state["conversation"]


@workflow.defn(name="SubagentParentProbeAgent")
@agent.defn
class SubagentParentProbeAgent:
    """Starts one subagent, so the policy it asks the server for is visible in its history.

    Carries the hooks as well, because a delegating session that cannot roll over cannot show
    what a rollover does to the subagents it is driving.
    """

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._started: list[str] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Start a subagent on the task queue named by the message and report its handle."""
        handle = await self._runner.start_subagent(
            "probe-child", "RolloverProbeAgent", message.text
        )
        self._started.append(handle)
        return TextReply(text=handle)

    @agent.snapshot
    def snapshot(self) -> dict[str, Any]:
        return {"started": self._started}

    @agent.restore
    def restore(self, state: dict[str, Any]) -> None:
        self._started = list(state["started"])


_AGENTS = [
    RolloverProbeAgent,
    ForgetfulProbeAgent,
    OwnStreamProbeAgent,
    SharedStreamProbeAgent,
    LeakyProbeAgent,
    OversizedProbeAgent,
    SubagentParentProbeAgent,
]


@asynccontextmanager
async def _environment(data_converter):
    env = await WorkflowEnvironment.start_time_skipping(data_converter=data_converter)
    task_queue = f"continue-as-new-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=_AGENTS,
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


@pytest_asyncio.fixture
async def client_and_queue():
    async with _environment(pydantic_data_converter) as running:
        yield running


@pytest_asyncio.fixture
async def offloading_client_and_queue(tmp_path, monkeypatch):
    """A stack wired the way every entry point in this repo wires one: payloads over the
    threshold are offloaded to external storage and replaced on the wire by a claim check."""
    monkeypatch.setattr(large_payload, "_BASE_DIR", tmp_path)
    converter = await large_payload.with_large_payload_offload(pydantic_data_converter)
    async with _environment(converter) as running:
        yield running


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _start(
    client: Client, task_queue: str, workflow_type: str = "RolloverProbeAgent"
) -> WorkflowHandle:
    return await client.start_workflow(
        workflow_type,
        AgentConfig(),
        id=f"rollover-probe-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def _send(
    client: Client, workflow_id: str, text: str, turn: int
) -> AgentMessageReply:
    """Queue one message and return once the session has accepted it as the given turn.

    Addresses the workflow id rather than a pinned run, which is how every real client reaches
    a session and therefore the only way the rollover can be shown to be invisible to one.
    """
    return await client.get_workflow_handle(workflow_id).execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="ask", payload={"text": text}, expected_turn=turn),
        result_type=AgentMessageReply,
    )


async def _reply_to(client: Client, workflow_id: str, reply: AgentMessageReply) -> str:
    """Read one accepted turn's reply text off the stream, starting where it was accepted."""
    events = WorkflowStreamClient.create(client, workflow_id).subscribe(
        TURN_EVENTS_TOPIC,
        from_offset=reply.accepted_offset,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=20),
    )
    async with asyncio.timeout(30), aclosing(events):
        async for item in events:
            if item.data.turn_id != reply.turn_id:
                continue
            if item.data.event.type == AgentEventType.REPLY:
                return str(item.data.event.output["text"])
            if item.data.event.type == AgentEventType.TURN_END:
                break
    raise AssertionError(f"turn {reply.turn_number} ended without a reply")


async def _say(client: Client, workflow_id: str, text: str, turn: int) -> str:
    """Send one message and return the agent's reply text."""
    return await _reply_to(client, workflow_id, await _send(client, workflow_id, text, turn))


async def _current_run_id(client: Client, workflow_id: str) -> str:
    return str((await client.get_workflow_handle(workflow_id).describe()).run_id)


async def _await_rollover(client: Client, workflow_id: str, original_run_id: str) -> str:
    """Wait until a different run is serving the session, and return that run's id."""
    async with asyncio.timeout(30):
        while True:
            run_id = await _current_run_id(client, workflow_id)
            if run_id != original_run_id:
                return run_id
            await asyncio.sleep(0.05)


def _rollover_warnings(caplog) -> list[str]:
    """Everything the workflow said about a session that will not continue as new."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and "continue as new" in record.getMessage()
    ]


async def _workflow_task_failures(handle: WorkflowHandle) -> list[str]:
    return [
        event.workflow_task_failed_event_attributes.failure.message
        async for event in handle.fetch_history_events()
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED
    ]


async def _child_start_policies(handle: WorkflowHandle) -> list[ParentClosePolicy]:
    return [
        event.start_child_workflow_execution_initiated_event_attributes.parent_close_policy
        async for event in handle.fetch_history_events()
        if event.event_type
        == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
    ]


# ---------------------------------------------------------------------------
# The end-to-end rollover
# ---------------------------------------------------------------------------


async def test_a_session_keeps_its_conversation_across_a_rollover(
    client_and_queue, rollover_after_one_turn
):
    """The whole point: a turn before the boundary and a turn after it, with the agent still
    knowing what was said. Without ``@agent.restore`` running on the successor the second reply
    would come back as just "bananas"; without the turn counter travelling with it, the second
    send would be rejected as stale before it got that far."""
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)

    assert await _say(client, handle.id, "apples", 1) == "apples"

    successor = await _await_rollover(client, handle.id, str(handle.result_run_id))
    assert successor != handle.result_run_id

    assert await _say(client, handle.id, "bananas", 2) == "apples bananas"


async def test_stream_offsets_keep_climbing_across_the_boundary(
    client_and_queue, rollover_after_one_turn
):
    """The consumer-facing half of the rollover, and the one that would fail silently.

    Every client reads this stream by global offset: the UI attaches with a ``from_offset`` it
    got from a previous read, and ``send_agent_message`` hands back an ``accepted_offset`` the
    merge starts from. Those offsets are the stream's own, not workflow history event ids, and
    they only stay meaningful because the log's ``base_offset`` travels in the resume state. Had
    the successor started a fresh stream at zero, an attached reader's cursor would point past
    the end of the new log and its poll would block until the session had generated that many
    events all over again.
    """
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)

    before = await _send(client, handle.id, "apples", 1)
    await _reply_to(client, handle.id, before)
    await _await_rollover(client, handle.id, str(handle.result_run_id))
    rollover_after_one_turn()

    after = await _send(client, handle.id, "bananas", 2)
    assert after.accepted_offset > before.accepted_offset
    assert await _reply_to(client, handle.id, after) == "apples bananas"


async def test_an_attached_stream_client_follows_the_handoff(
    client_and_queue, rollover_after_one_turn
):
    """The failure a person would notice first. One subscriber, opened before the rollover and
    never restarted, has to see both turns even though the run it was polling closes under it."""
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    seen: list[tuple[int, str]] = []

    async def follow() -> None:
        async for item in WorkflowStreamClient.create(client, handle.id).subscribe(
            TURN_EVENTS_TOPIC,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=20),
        ):
            seen.append((item.data.turn_number, item.data.event.type))

    follower = asyncio.create_task(follow())
    try:
        await _say(client, handle.id, "apples", 1)
        await _await_rollover(client, handle.id, str(handle.result_run_id))
        await _say(client, handle.id, "bananas", 2)
        async with asyncio.timeout(30):
            while (2, AgentEventType.TURN_END) not in seen:
                await asyncio.sleep(0.05)
    finally:
        follower.cancel()

    assert (1, AgentEventType.TURN_END) in seen


async def test_a_message_accepted_just_before_the_boundary_is_still_answered(
    client_and_queue, rollover_after_one_turn
):
    """A queued message is work the session already promised: ``send_agent_message`` returned an
    accepted turn number, so its caller believes the turn will run. The queue is therefore
    carried whole rather than reconstructed — dropping it would lose a message the client was
    told had landed, and renumbering it would make every later ``expected_turn`` wrong."""
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)

    await _send(client, handle.id, "apples", 1)
    # Queued behind turn 1, which is the state the rollover has to carry. Both turns are sent
    # before anything is read, so the second is admitted while the first is still in flight.
    queued = await _send(client, handle.id, "bananas", 2)

    await _await_rollover(client, handle.id, str(handle.result_run_id))
    rollover_after_one_turn()

    assert await _reply_to(client, handle.id, queued) == "apples bananas"


async def test_a_client_is_carried_across_even_when_nothing_of_the_log_survives(
    client_and_queue, rollover_after_one_turn, monkeypatch
):
    """The carried transcript tail is bounded, and at a budget of zero every prior event is
    dropped. A client polling at the head must still be carried: offsets stay monotonic through
    a truncation, so its position remains valid even when nothing before it survives.

    Turn one is only awaited as far as the rollover it triggers, because at this budget its
    reply may well be truncated away before anything gets to read it — which is the condition
    under test, not a flake. Turn two then needs a run that will stay put long enough to be
    read, so the suggestion is withdrawn once the one rollover under test has happened."""
    client, task_queue = client_and_queue
    monkeypatch.setattr(agent_workflow, "_STREAM_HANDOVER_BUDGET_BYTES", 0)
    handle = await _start(client, task_queue)

    await _send(client, handle.id, "apples", 1)
    await _await_rollover(client, handle.id, str(handle.result_run_id))
    rollover_after_one_turn()

    assert await _say(client, handle.id, "bananas", 2) == "apples bananas"


async def test_a_conversation_too_big_for_one_payload_still_crosses(
    offloading_client_and_queue, rollover_after_one_turn
):
    """A carried conversation is a payload, and Temporal caps a single payload at about 2 MB.
    Nothing in the rollover path handles that, on purpose: the harness's data converter already
    offloads anything over its threshold to external storage at every connect site, and it does
    so beneath the workflow, so continue-as-new input is covered like any other payload. This is
    that premise under test rather than assumed — the blob on disk is the evidence it took the
    offloading path, since the test server enforces no size limit of its own and would happily
    have accepted the whole thing inline."""
    client, task_queue = offloading_client_and_queue
    handle = await _start(client, task_queue, "OversizedProbeAgent")

    assert await _say(client, handle.id, "x", 1) == str(_OVERSIZED_BYTES)
    await _await_rollover(client, handle.id, str(handle.result_run_id))
    rollover_after_one_turn()

    assert await _say(client, handle.id, "x", 2) == str(_OVERSIZED_BYTES * 2)
    assert list(large_payload._BASE_DIR.glob("*.bin"))


async def test_a_snapshot_that_is_not_json_native_stops_the_rollover_and_says_why(
    client_and_queue, rollover_after_one_turn
):
    """The contract's one silent failure, closed. A snapshot holding SDK objects rides the
    pydantic converter without complaint and arrives in the successor as plain dicts, so the
    mistake shows up one run later as an agent whose conversation has quietly changed shape.

    Checked where the blob is taken instead, which puts the failure on the workflow task: the
    session pauses with the reason on its pending task, retries indefinitely, and picks the same
    conversation back up once the hook is fixed and the worker redeployed. Nothing is lost, which
    is exactly what is not true of rolling over with a blob that degrades on the way. The turn
    before the boundary is still answered — this run is intact; it simply cannot leave.
    """
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, "LeakyProbeAgent")

    # ``_send`` rather than ``_say``: reading the reply off the stream means polling the workflow,
    # and this is the one session in the file whose workflow task is about to start failing, so a
    # poll would be waiting on a run that cannot answer it.
    await _send(client, handle.id, "apples", 1)

    async with asyncio.timeout(30):
        while not (failures := await _workflow_task_failures(handle)):
            await asyncio.sleep(0.05)

    assert "not JSON-native" in failures[0]
    assert "state['conversation'][0] is a TextReply" in failures[0]
    # And it did not hand the successor the degraded blob instead.
    assert await _current_run_id(client, handle.id) == str(handle.result_run_id)


# ---------------------------------------------------------------------------
# The vetoes
# ---------------------------------------------------------------------------


async def test_an_agent_without_the_hooks_never_rolls_over(
    client_and_queue, rollover_after_one_turn
):
    """Degrading to "cannot roll over" is the deliberate choice. A session whose history keeps
    growing fails loudly, later; one that rolled over having forgotten the conversation fails
    silently, now, in front of the person it forgot."""
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, "ForgetfulProbeAgent")

    await _say(client, handle.id, "apples", 1)
    await _say(client, handle.id, "bananas", 2)

    assert await _current_run_id(client, handle.id) == str(handle.result_run_id)


async def test_an_agent_that_supplies_its_own_stream_never_rolls_over(
    client_and_queue, rollover_after_one_turn
):
    """Handing over stream state means constructing the successor's WorkflowStream with it,
    which the runner can only do for a stream it built. Declaring the hooks is not enough."""
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, "OwnStreamProbeAgent")

    await _say(client, handle.id, "apples", 1)
    await _say(client, handle.id, "bananas", 2)

    assert await _current_run_id(client, handle.id) == str(handle.result_run_id)


async def test_the_hooks_plus_a_supplied_stream_is_reported_when_the_session_starts(
    client_and_queue, caplog
):
    """The veto above is correct and invisible: an author who wrote the pair believes they opted
    in, and nothing contradicts them until a conversation gets long. But both facts are already
    in hand when the runner is built — the class declares the pair, this runner did not build the
    stream — so it is said on the session's first turn instead of hours later.

    Deliberately a log line and not a ``TypeError``. The runner is constructed inside
    ``@workflow.init``; raising there fails the workflow task, Temporal retries a failed workflow
    task forever, and every live session of a deployed agent would hang with no opt-out but
    deleting the hooks. Which is why this test asserts the session still works.

    No rollover is asked for here — that is the point. The existing per-turn veto log needs the
    session to be over Temporal's threshold; this one does not.
    """
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, "OwnStreamProbeAgent")

    with caplog.at_level(logging.WARNING, logger="temporalio.workflow"):
        assert await _say(client, handle.id, "apples", 1) == "apples"

    said = _rollover_warnings(caplog)
    assert len(said) == 1
    assert "stream it did not create" in said[0]
    assert "Drop the stream= argument" in said[0]


@pytest.mark.parametrize(
    "workflow_type",
    ["RolloverProbeAgent", "SharedStreamProbeAgent", "ForgetfulProbeAgent"],
)
async def test_nothing_is_said_about_an_agent_that_has_not_contradicted_itself(
    client_and_queue, caplog, workflow_type
):
    """A warning every agent earns is one nobody reads, so all three of the non-contradictions
    stay silent: the pair with a runner-built stream (what a rolling agent does), a supplied
    stream with no pair (the documented trade), and neither (nothing to carry)."""
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, workflow_type)

    with caplog.at_level(logging.WARNING, logger="temporalio.workflow"):
        await _say(client, handle.id, "apples", 1)

    assert _rollover_warnings(caplog) == []


# ---------------------------------------------------------------------------
# Subagents across the boundary
# ---------------------------------------------------------------------------


async def test_a_subagent_is_started_to_survive_its_parents_rollover(client_and_queue):
    """Continue-as-new is a parent close, so under the TERMINATE this replaces, a parent's
    first rollover would kill every subagent it was driving, mid-conversation.

    Asserted on the StartChildWorkflowExecutionInitiated event rather than on a surviving
    child, because the time-skipping test server does not enforce parent-close policies at all
    — a survival test would pass under either policy and prove nothing. What this code decides
    is what it asks the server for; honouring the request is Temporal's contract.
    """
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, "SubagentParentProbeAgent")

    await _say(client, handle.id, task_queue, 1)

    assert await _child_start_policies(handle) == [
        ParentClosePolicy.PARENT_CLOSE_POLICY_ABANDON
    ]


async def test_a_rollover_carries_its_subagents_rather_than_stopping_them(
    client_and_queue, rollover_after_one_turn
):
    """The other exit a delegating session can take, and the one that must NOT tear anything
    down. Every other way out of the turn loop owes its subagents a ``close`` and a
    ``subagent_stopped`` — the conversation is over. A rollover is the same conversation on a new
    run, which re-adopts the children by workflow id, so stopping them here would kill a live
    subagent mid-conversation and tell an attached console to unmount a stream it will keep
    receiving. Watched from a subscriber that spans the boundary, since a stop record published
    on the old run is exactly what that console would act on.
    """
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, "SubagentParentProbeAgent")
    seen: list[str] = []

    async def follow() -> None:
        async for item in WorkflowStreamClient.create(client, handle.id).subscribe(
            TURN_EVENTS_TOPIC,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=20),
        ):
            seen.append(item.data.event.type)

    follower = asyncio.create_task(follow())
    try:
        child = await _say(client, handle.id, task_queue, 1)
        await _await_rollover(client, handle.id, str(handle.result_run_id))
        rollover_after_one_turn()
        async with asyncio.timeout(30):
            while AgentEventType.TURN_END not in seen:
                await asyncio.sleep(0.05)
    finally:
        follower.cancel()

    assert AgentEventType.SUBAGENT_STOPPED not in seen
    status = await client.get_workflow_handle(handle.id).query(
        AGENT_STATUS_QUERY, result_type=AgentStatus
    )
    assert [info.subagent_id for info in status.subagents] == [child]
