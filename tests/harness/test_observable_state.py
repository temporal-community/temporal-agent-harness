# ABOUTME: Tests for observable agent state on the turn_events stream — that a workflow
# author's single `runner.state(...)` call is the whole opt-in, that the runner publishes a
# snapshot at registration and one patch per committing mutate() block without the author
# asking, that the ops are stamped with the turn they happened in, and that replaying the
# stream reproduces the agent's state exactly.
#
# Run end-to-end against the Temporal time-skipping test server, because the thing under
# test is precisely that these events survive the workflow -> stream -> client round trip.
#
# Run with: uv run pytest tests/harness/test_observable_state.py -v

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import jsonpatch
import pytest
import pytest_asyncio
from temporalio import workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
from temporal_agent_harness.harness.state import HarnessState, StateRef

# ---------------------------------------------------------------------------
# An author's state, and an agent that opts in
# ---------------------------------------------------------------------------


class Step(HarnessState):
    name: str
    done: bool = False


class PlanState(HarnessState):
    goal: str = ""
    steps: list[Step] = []
    scratch: dict[str, str] = {}


@workflow.defn(name="StateProbeAgent")
@agent.defn
class StateProbeAgent:
    """An agent whose entire state-tracking opt-in is one line in __init__."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # THE OPT-IN. Nothing below ever mentions events, topics or publishing.
        self._plan: StateRef[PlanState] = self._runner.state("plan", PlanState())

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def plan(self, message: TextMessage) -> TextReply:
        """Build a plan out of the message, mutating tracked state as it goes."""
        with self._plan.mutate() as d:
            d.goal = message.text
            d.steps.append(Step(name="research"))
            d.steps.append(Step(name="write"))

        with self._plan.mutate() as d:
            d.steps[0].done = True
            d.scratch["note"] = "research finished"

        with self._plan.mutate() as d:
            pass  # touches nothing: must publish nothing and burn no version

        with self._plan.mutate() as d:
            del d.steps[0]

        return TextReply(text=f"{len(self._plan.current.steps)} step(s) left")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"observable-state-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[StateProbeAgent],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _run_turn(client: Client, task_queue: str) -> WorkflowHandle:
    handle = await client.start_workflow(
        StateProbeAgent.run,
        AgentConfig(),
        id=f"StateProbeAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(
            type="plan",
            payload={"text": "ship the feature"},
            expected_turn=1,
        ),
        result_type=AgentMessageReply,
    )
    return handle


async def _collect(client: Client, workflow_id: str) -> list[AgentEvent]:
    stream = WorkflowStreamClient.create(client, workflow_id)
    events: list[AgentEvent] = []
    async with asyncio.timeout(60):
        async for item in stream.subscribe(
            topics=["turn_events"],
            from_offset=0,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            events.append(item.data)
            if item.data.event.type == AgentEventType.TURN_END:
                break
    return events


@pytest_asyncio.fixture
async def events(client_and_queue) -> list[AgentEvent]:
    client, task_queue = client_and_queue
    handle = await _run_turn(client, task_queue)
    return await _collect(client, handle.id)


def _state_events(events: list[AgentEvent], kind: AgentEventType) -> list[Any]:
    return [e.event for e in events if e.event.type == kind]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_registration_publishes_one_snapshot_outside_any_turn(events):
    snapshots = [e for e in events if e.event.type == AgentEventType.STATE_SNAPSHOT]
    assert len(snapshots) == 1

    envelope = snapshots[0]
    assert envelope.event.state_id == "plan"
    assert envelope.event.version == 0
    assert envelope.event.value == {"goal": "", "steps": [], "scratch": {}}
    # Registered in @workflow.init, so there is no turn to attribute it to; the runner
    # follows the operator-command convention rather than inventing a turn.
    assert envelope.turn_number == 0
    assert events.index(snapshots[0]) == 0, "the snapshot must precede every patch"


async def test_each_committing_mutate_publishes_exactly_one_patch(events):
    patches = _state_events(events, AgentEventType.STATE_PATCH)
    # Three committing blocks; the empty `with` block publishes nothing at all.
    assert len(patches) == 3
    assert [p.version for p in patches] == [1, 2, 3]
    assert all(p.state_id == "plan" for p in patches)

    assert [op["op"] for op in patches[0].ops] == ["replace", "add", "add"]
    assert patches[0].ops[0] == {"op": "replace", "path": "/goal", "value": "ship the feature"}
    assert patches[1].ops == [
        {"op": "replace", "path": "/steps/0/done", "value": True},
        {"op": "add", "path": "/scratch/note", "value": "research finished"},
    ]
    assert patches[2].ops == [{"op": "remove", "path": "/steps/0"}]


async def test_patches_are_stamped_with_the_turn_they_happened_in(events):
    patch_envelopes = [e for e in events if e.event.type == AgentEventType.STATE_PATCH]
    turn_starts = [e for e in events if e.event.type == AgentEventType.TURN_STARTED]
    assert turn_starts, "expected a turn"
    turn_id = turn_starts[0].turn_id

    assert all(e.turn_id == turn_id for e in patch_envelopes)
    assert all(e.turn_number == 1 for e in patch_envelopes)
    assert all(e.agent_id for e in patch_envelopes)


async def test_state_changes_are_ordered_against_the_rest_of_the_turn(events):
    """The reason for riding turn_events: a consumer can see *when* state moved."""
    kinds = [e.event.type for e in events]
    first_patch = kinds.index(AgentEventType.STATE_PATCH)
    assert kinds.index(AgentEventType.TURN_STARTED) < first_patch
    assert first_patch < kinds.index(AgentEventType.TURN_END)


async def test_replaying_the_stream_reproduces_the_agents_state(events):
    """The invariant the whole design rests on, end to end through Temporal."""
    snapshot = next(e.event for e in events if e.event.type == AgentEventType.STATE_SNAPSHOT)
    document = snapshot.value
    for patch in _state_events(events, AgentEventType.STATE_PATCH):
        document = jsonpatch.apply_patch(document, patch.ops)

    assert document == {
        "goal": "ship the feature",
        "steps": [{"name": "write", "done": False}],
        "scratch": {"note": "research finished"},
    }


async def test_duplicate_state_ids_are_rejected():
    """A second registration under the same id is a programming error, not a merge."""
    runner = AgentWorkflowRunner.__new__(AgentWorkflowRunner)
    runner._states = {}
    runner._agent_id = "a1"
    published: list[Any] = []
    runner._publish_state_event = published.append  # type: ignore[method-assign]

    ref = AgentWorkflowRunner.state(runner, "plan", PlanState())
    assert isinstance(ref, StateRef)
    assert len(published) == 1

    with pytest.raises(ValueError, match="already registered"):
        AgentWorkflowRunner.state(runner, "plan", PlanState())
