# ABOUTME: Tests for observable agent state on the turn_events stream — that a workflow
# author's single `runner.state(...)` call is the whole opt-in, that the runner publishes a
# snapshot at registration and one patch per committing mutate() block without the author
# asking, that the ops are stamped with the turn they happened in, and that replaying the
# stream reproduces the agent's state exactly.
#
# Run end-to-end against the Temporal time-skipping test server, because the thing under
# test is precisely that these events survive the workflow -> stream -> client round trip.
#
# The last section runs the state layer under the DEFAULT (sandboxed) workflow runner, which
# the agent tests above deliberately do not: `mutate()` generates a `Draft[C]` class with
# `type()` *inside* a workflow task, and the sandbox is where that either works or does not.
#
# Run with: uv run pytest tests/harness/test_observable_state.py -v

from __future__ import annotations

import asyncio
import gc
import uuid
from datetime import timedelta
from typing import Any

from temporalio import workflow

# The sandboxed workflow at the bottom lives in this module, so the Temporal sandbox
# re-imports this whole file once per workflow instance. Everything the file imports is
# passed through so that re-import stays cheap and the only things the sandbox actually
# rebuilds are this module's own classes -- which is the behaviour under test.
with workflow.unsafe.imports_passed_through():
    import jsonpatch
    import pytest
    import pytest_asyncio
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
    from temporal_agent_harness.harness.agent_client import AgentClient
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
    from temporal_agent_harness.harness.state import HarnessState, StateRef
    from temporal_agent_harness.harness.state import drafts as _drafts

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


async def test_attach_to_a_brand_new_session_delivers_the_snapshot_and_stops(
    client_and_queue,
):
    """A fresh session's entire history is one turn-0 snapshot, and attach must finish on it.

    `should_stop` would only ask whether the workflow was idle on a *terminal* root event --
    a `turn_end`, or an operator command's terminal -- and a `state_snapshot` is neither. So
    the moment registering state put an event at offset 0, attach delivered it and then
    blocked forever waiting for a `turn_end` that could not arrive until somebody sent a
    message.

    In the console that closed a loop: `creatingSession` is cleared when the first attach
    goes idle, and it gates the composer, so the session the user had just created could not
    be sent anything -- and with nothing sent, nothing would ever end the attach. A reload
    was the only way out, because that path never sets the flag.

    The timeout is the assertion. Before the fix this test hangs rather than failing, which
    is also exactly how the bug presented.
    """
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        StateProbeAgent.run,
        AgentConfig(),
        id=f"StateProbeAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    agent_client = AgentClient(client, handle.id)

    stream = await agent_client.attach(
        from_offset=0,
        on_item=lambda item, _resume_offset: item,
    )
    items: list[AgentEvent] = []
    async with asyncio.timeout(10):
        async for item in stream:
            items.append(item)

    assert [item.event.type for item in items] == [AgentEventType.STATE_SNAPSHOT]
    assert items[0].event.version == 0
    assert items[0].turn_number == 0


# ---------------------------------------------------------------------------
# The same state layer, under the DEFAULT (sandboxed) workflow runner
# ---------------------------------------------------------------------------
#
# `mutate()` calls `drafts.draft_class()`, which builds a `Draft[C]` subclass of the
# author's class with `type()` — inside a workflow task. Everything above runs
# unsandboxed, so none of it says whether that is allowed where real agents run. These
# workflows carry no agent runner: the sandbox is the only variable.


class SandboxPlan(HarnessState):
    """Declared in this module on purpose: the sandbox re-imports the workflow's own
    module per workflow instance, so this class object is NEW for every run."""

    goal: str = ""
    steps: list[Step] = []
    scratch: dict[str, str] = {}


@workflow.defn(name="SandboxStateProbe")
class SandboxStateProbe:
    @workflow.run
    async def run(self, goal: str) -> dict[str, Any]:
        ops: list[dict[str, Any]] = []
        ref: StateRef[SandboxPlan] = StateRef(
            "plan", SandboxPlan(), publish=lambda event: ops.extend(event.ops)
        )
        snapshot = ref.snapshot().value

        with ref.mutate() as d:
            d.goal = goal
            d.steps.append(Step(name="research"))
            d.steps.append(Step(name="write"))
        with ref.mutate() as d:
            d.steps[0].done = True
            d.scratch["note"] = "research finished"
        with ref.mutate() as d:
            del d.steps[0]

        draft_cls = _drafts.draft_class(SandboxPlan)
        return {
            "snapshot": snapshot,
            "ops": ops,
            "committed": ref.snapshot().value,
            "version": ref.version,
            # Proof the generated subclass is the thing that did the work, built here.
            "draft_class_name": draft_cls.__name__,
            "draft_is_subclass": issubclass(draft_cls, SandboxPlan),
            "draft_at_rest_is_state_class": draft_cls.__harness_at_rest__ is SandboxPlan,
            # The draft class must hang off the state class and nowhere else, so that it
            # dies with the module the sandbox re-imported for this workflow instance.
            "draft_on_state_class": (
                SandboxPlan.__dict__.get("__harness_draft_class__") is draft_cls
            ),
        }


@pytest_asyncio.fixture
async def sandboxed_queue():
    """A worker on the DEFAULT workflow runner — i.e. the real sandbox."""
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"sandboxed-state-test-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=task_queue, workflows=[SandboxStateProbe]):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _run_sandboxed(client: Client, task_queue: str, goal: str) -> dict[str, Any]:
    return await client.execute_workflow(
        SandboxStateProbe.run,
        goal,
        id=f"SandboxStateProbe-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def test_mutation_works_under_the_sandboxed_runner(sandboxed_queue):
    """Generating `Draft[C]` with `type()` is allowed inside a workflow task.

    The sandbox swaps in its own import hooks and a restricted `builtins`; if either
    objected to building a class at workflow time — or to pydantic's metaclass running
    there — every mutate() block in every real agent would fail. Nothing above catches
    that, because everything above opts out of the sandbox.
    """
    client, task_queue = sandboxed_queue
    result = await _run_sandboxed(client, task_queue, "ship the feature")

    assert result["draft_class_name"] == "Draft[SandboxPlan]"
    assert result["draft_is_subclass"]
    assert result["draft_at_rest_is_state_class"]
    assert result["draft_on_state_class"]
    assert result["version"] == 3
    assert result["committed"] == {
        "goal": "ship the feature",
        "steps": [{"name": "write", "done": False}],
        "scratch": {"note": "research finished"},
    }


async def test_sandboxed_ops_replay_to_the_sandboxed_agents_state(sandboxed_queue):
    """The layer's invariant, checked where the draft classes are actually generated."""
    client, task_queue = sandboxed_queue
    result = await _run_sandboxed(client, task_queue, "ship the feature")

    document = result["snapshot"]
    for op in result["ops"]:
        document = jsonpatch.apply_patch(document, [op])
    assert document == result["committed"]


async def test_two_sandboxed_instances_agree_op_for_op(sandboxed_queue):
    """Each instance gets a fresh `SandboxPlan`, so each generates its own `Draft[C]`.

    The generated class must therefore be a pure function of its input: two workflow
    instances on one worker have to publish byte-identical ops, or a replay on a second
    worker diverges from the history.
    """
    client, task_queue = sandboxed_queue
    first = await _run_sandboxed(client, task_queue, "ship the feature")
    second = await _run_sandboxed(client, task_queue, "ship the feature")

    assert first["ops"] == second["ops"]
    assert first["snapshot"] == second["snapshot"]
    assert first["committed"] == second["committed"]


def _live_draft_classes() -> int:
    """How many generated `Draft[...]` classes this process is still holding.

    Counted out of the garbage collector rather than out of a cache, because the fix for
    the leak was to stop having a cache: `Draft[C]` hangs off `C`, so the only honest
    question is whether the class objects survive. `C` and `Draft[C]` refer to each
    other, so collecting them is a cycle collection, not a refcount drop — hence the
    explicit passes.
    """
    for _ in range(3):
        gc.collect()
    return sum(
        1
        for obj in gc.get_objects()
        if isinstance(obj, type) and obj.__dict__.get("__harness_is_draft__", False)
    )


async def test_the_draft_class_cache_does_not_grow_per_workflow_instance(sandboxed_queue):
    """`Draft[C]` costs per state CLASS; a worker must not pay per workflow RUN.

    `SandboxPlan` is declared in the workflow's own module, so the sandbox builds a
    brand-new class object for every workflow instance — the pattern a state class
    declared next to its `@workflow.defn` gets by default. Anything that retains those
    classes (a module-level `dict[type, type]` keyed on them, say) turns one run's
    garbage into a permanent ~107 kB of RSS and never evicts it: an OOM, not a plateau.

    Measured as a plateau rather than an absolute count, because the sandbox does hold
    onto one module copy of its own; the claim under test is that traffic adds nothing
    on top of it. Two equal batches, and the second must add nothing.

    Asserted as `<= 0` and not `== 0`. A DECREASE is not a failure — it means classes
    the first batch still had alive when `warm` was sampled were reclaimed later, so
    `warm` was simply an over-count. Whether a given `C`/`Draft[C]` cycle is reclaimed
    on the third `gc.collect()` or a little after it depends on when the last frame
    referencing it goes away, and 3.13/3.14 answer that differently from 3.11/3.12 —
    `== 0` failed intermittently there with growth of -3 for exactly that reason. The
    leak this guards against is unbounded GROWTH: a retained class per run shows up as
    roughly +`runs`, which this still catches.
    """
    client, task_queue = sandboxed_queue

    runs = 5
    for _ in range(runs):
        await _run_sandboxed(client, task_queue, "warm the process")
    warm = _live_draft_classes()

    for _ in range(runs):
        await _run_sandboxed(client, task_queue, "again")

    growth = _live_draft_classes() - warm
    assert growth <= 0, (
        f"{runs} more workflow instances left {growth} more Draft[...] classes alive "
        f"({growth / runs:.1f} per instance): generated classes are accumulating with "
        f"traffic rather than with the number of declared state classes"
    )
