# ABOUTME: Tests for the AgentWorkflowRunner handler-dispatch model — that @agent.accepts
# handlers are discovered and validated, that an inbound send_agent_message envelope routes
# by `type` to the matching handler (reconstructing its input model, rejecting an unknown
# function or a malformed payload at the update boundary), that the handler's return value
# is published as the reply, that the agent_interface query announces the callable surface,
# and that the runner resolves config-vs-agent-default knobs (with stream + approval policy
# required).
#
# The accept/reject/dispatch behavior is exercised end-to-end through real updates against
# the Temporal time-skipping test server (the only faithful way — routing lives in the
# update validator + run loop, which run in a workflow context). The discovery/validation
# and config-resolution checks are plain unit tests.
#
# Run with: uv run pytest tests/harness/test_agent_workflow_runner.py -v

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import Client, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    AGENT_INTERFACE_QUERY,
    AGENT_STATUS_QUERY,
    SEND_AGENT_MESSAGE_UPDATE,
    AcceptedFunction,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentReply,
    AgentStatus,
    MessageContext,
    MidTurn,
    SubagentReplyReceived,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
    AgentMessageReply,
)
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_workflow import Injected, _discover_handlers

# ---------------------------------------------------------------------------
# Message models + probe workflows
# ---------------------------------------------------------------------------


class Greeting(BaseModel):
    """A person to greet."""

    name: str


class Greeted(BaseModel):
    """The greeting produced for a person."""

    message: str


class ModelPick(BaseModel):
    """A model selection."""

    model: str


class Picked(BaseModel):
    """The confirmed model selection."""

    model: str


@workflow.defn
@agent.defn
class TypedProbeAgent:
    """Two handlers — greet(Greeting)->Greeted and pick(ModelPick)->Picked — plus a
    failing handler. Records each handled message so a test can confirm the runner routed
    + reconstructed the concrete input model (not a dict)."""

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

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def greet(self, message: Greeting) -> Greeted:
        """Greet a person by name."""
        self._seen.append(f"greet:{message.name}")
        return Greeted(message=f"hi {message.name}")

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def pick(self, message: ModelPick) -> Picked:
        """Pick a model for the session."""
        self._seen.append(f"pick:{message.model}")
        return Picked(model=message.model)

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def boom(self, message: TextMessage) -> TextReply:
        """Always raises — to prove an errored turn publishes AgentError + turn_end and
        the loop survives for the next message."""
        raise RuntimeError(f"boom: {message.text}")

    @workflow.query
    def seen(self) -> list[str]:
        return self._seen


@workflow.defn
@agent.defn
class MidTurnProbeAgent:
    """Covers all three ``mid_turn`` modes plus ``Injected[MessageContext]``.

    ``work`` blocks until signalled, so a test can hold a turn open and observe what happens
    to a message that arrives while it is running."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._released: set[str] = set()
        self._joined: list[bool] = []
        self._published_after_sibling = False

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @workflow.signal
    def release(self, which: str) -> None:
        self._released.add(which)

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def work(self, message: TextMessage) -> TextReply:
        """Long-running work — holds its turn open until released."""
        await workflow.wait_condition(lambda: "work" in self._released)
        return TextReply(text=f"worked:{message.text}")

    @agent.accepts(mid_turn=MidTurn.ACCEPT)
    async def steer(
        self, message: TextMessage, ctx: Injected[MessageContext]
    ) -> TextReply:
        """Joins an open turn, or opens one when idle — and records which case it was."""
        self._joined.append(ctx.joined_turn)
        return TextReply(
            text=f"steer:{message.text}:joined={ctx.joined_turn}:turn={ctx.turn_number}"
        )

    @agent.accepts(mid_turn=MidTurn.ACCEPT)
    async def outlive(self, message: TextMessage) -> TextReply:
        """Joins a turn, waits for the OPENER to finish, then publishes against the turn.

        The regression this guards: if the turn id were cleared when the first participant
        finished, this publish would hard-raise instead of landing on the still-open turn."""
        await workflow.wait_condition(lambda: "outlive" in self._released)
        self._runner.publish(AgentReply(output={"note": "still in the turn"}))
        self._published_after_sibling = True
        return TextReply(text=f"outlived:{message.text}")

    @agent.accepts(mid_turn=MidTurn.REJECT)
    async def exclusive(self, message: TextMessage) -> TextReply:
        """Must not pile up behind in-flight work."""
        return TextReply(text=f"exclusive:{message.text}")

    @workflow.query
    def joined(self) -> list[bool]:
        return self._joined

    @workflow.query
    def published_after_sibling(self) -> bool:
        return self._published_after_sibling


@pytest_asyncio.fixture
async def client_and_queue():
    """A time-skipping env (pydantic converter) with a worker hosting the probe."""
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"agent-workflow-runner-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[TypedProbeAgent, MidTurnProbeAgent],
        # Unsandboxed so the test module's imports (pydantic, harness, pytest) don't
        # trip the workflow sandbox; the runner logic under test is unaffected.
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _start(client: Client, task_queue: str, wf: Any) -> WorkflowHandle:
    return await client.start_workflow(
        wf.run, AgentConfig(), id=f"{wf.__name__}-{uuid.uuid4()}", task_queue=task_queue
    )


async def _next_expected_turn(handle: WorkflowHandle) -> int:
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    return status.current_turn + len(status.pending_turns) + 1


async def _send(
    handle: WorkflowHandle, type: str, payload: dict[str, Any]
) -> AgentMessageReply:
    return await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(
            type=type,
            payload=payload,
            expected_turn=await _next_expected_turn(handle),
        ),
        result_type=AgentMessageReply,
    )


async def _wait_for_seen(
    handle: WorkflowHandle, count: int, *, attempts: int = 200, delay: float = 0.05
) -> list[str]:
    seen: list[str] = []
    for _ in range(attempts):
        seen = await handle.query("seen", result_type=list[str])
        if len(seen) >= count:
            return seen
        await asyncio.sleep(delay)
    raise AssertionError(f"timed out waiting for {count} seen entries; got {seen}")


# ---------------------------------------------------------------------------
# Routing + dispatch (end-to-end)
# ---------------------------------------------------------------------------


async def test_routes_by_type_and_reconstructs_input(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    await _send(handle, "greet", {"name": "Ada"})
    await _send(handle, "pick", {"model": "opus"})

    seen = await _wait_for_seen(handle, 2)
    # FIFO order, and each message arrived at its handler as the concrete input model.
    assert seen == ["greet:Ada", "pick:opus"]


async def test_rejects_unknown_function(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    with pytest.raises(WorkflowUpdateFailedError) as excinfo:
        await _send(handle, "does_not_exist", {})
    cause = excinfo.value.cause
    assert getattr(cause, "type", None) == "UnknownFunction"
    # The rejection spells out the known functions so a caller can self-correct.
    detail = str(cause)
    assert "greet" in detail and "pick" in detail

    # The rejected message created no turn.
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.current_turn == 0 and status.pending_turns == []


async def test_rejects_malformed_payload(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    # `greet` requires {name: str}; an empty payload fails its input model.
    with pytest.raises(WorkflowUpdateFailedError) as excinfo:
        await _send(handle, "greet", {})
    cause = excinfo.value.cause
    assert getattr(cause, "type", None) == "MalformedMessage"
    assert "Greeting" in str(cause)


async def test_agent_interface_query_announces_handlers(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    functions = await handle.query(
        AGENT_INTERFACE_QUERY, result_type=list[AcceptedFunction]
    )
    by_name = {f.name: f for f in functions}
    assert set(by_name) == {"greet", "pick", "boom"}
    # Description is the handler docstring; parameters/output are the model schemas.
    assert by_name["greet"].description == "Greet a person by name."
    assert "name" in by_name["greet"].parameters["properties"]
    assert "message" in by_name["greet"].output["properties"]


async def _collect_until_turn_end(client: Client, workflow_id: str) -> list[AgentEvent]:
    from datetime import timedelta

    from temporalio.contrib.workflow_streams import WorkflowStreamClient

    stream = WorkflowStreamClient.create(client, workflow_id)
    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
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


def _reply_text(events: list[AgentEvent]) -> str:
    reply = next(e.event for e in events if e.event.type == AgentEventType.REPLY)
    text = reply.output.get("text")
    assert isinstance(text, str)
    return text


async def test_reply_is_the_handler_return_value(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)
    await _send(handle, "greet", {"name": "Ada"})

    events = await _collect_until_turn_end(client, handle.id)
    reply = next(e.event for e in events if e.event.type == AgentEventType.REPLY)
    # The reply carries the handler's return model serialized to a dict.
    assert reply.output == {"message": "hi Ada"}


async def test_handler_error_publishes_agent_error_and_loop_survives(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    # A raising handler → AgentError (then turn_end), and the session stays alive.
    await _send(handle, "boom", {"text": "x"})
    events = await _collect_until_turn_end(client, handle.id)
    errors = [e for e in events if e.event.type == AgentEventType.ERROR]
    assert len(errors) == 1 and "boom: x" in errors[0].event.message
    # The next message is still handled normally.
    await _send(handle, "greet", {"name": "Bob"})
    seen = await _wait_for_seen(handle, 1)
    assert "greet:Bob" in seen


# ---------------------------------------------------------------------------
# Handler discovery + validation (pure unit tests)
# ---------------------------------------------------------------------------


def test_discovers_accepts_handlers():
    handlers = _discover_handlers(TypedProbeAgent)
    assert set(handlers) == {"greet", "pick", "boom"}
    assert handlers["greet"].input_type is Greeting
    assert handlers["greet"].output_type is Greeted


def test_discover_rejects_non_pydantic_input():
    class Bad:
        @agent.accepts
        async def h(self, message: int) -> Greeted:  # input not a pydantic model
            """h."""
            ...

    with pytest.raises(TypeError, match="must be annotated with a pydantic model"):
        _discover_handlers(Bad)


def test_discover_rejects_scalar_return():
    class Bad:
        @agent.accepts
        async def h(self, message: Greeting) -> str:  # scalar return
            """h."""
            ...

    with pytest.raises(TypeError, match="return type must be a pydantic model"):
        _discover_handlers(Bad)


def test_discover_rejects_missing_docstring():
    class Bad:
        @agent.accepts
        async def h(self, message: Greeting) -> Greeted:
            ...  # no docstring

    with pytest.raises(TypeError, match="must have a docstring"):
        _discover_handlers(Bad)


def test_discover_rejects_wrong_arity():
    class Bad:
        @agent.accepts
        async def h(self, a: Greeting, b: Greeting) -> Greeted:  # two args
            """h."""
            ...

    with pytest.raises(TypeError, match="exactly one argument"):
        _discover_handlers(Bad)


# ---------------------------------------------------------------------------
# @agent.defn signature contract
# ---------------------------------------------------------------------------


class _ValidAgentShape:
    @workflow.run
    async def run(self, config: AgentConfig) -> None: ...


class _MissingConfigShape:
    @workflow.run
    async def run(self) -> None: ...


class _WrongInputShape:
    @workflow.run
    async def run(self, value: int) -> None: ...


def test_agent_defn_accepts_single_agentconfig():
    assert agent.defn(_ValidAgentShape) is _ValidAgentShape


def test_agent_defn_rejects_missing_config_at_definition_time():
    with pytest.raises(TypeError, match="must accept exactly one AgentConfig"):
        agent.defn(_MissingConfigShape)


def test_agent_defn_rejects_bespoke_input_at_definition_time():
    with pytest.raises(TypeError, match="must accept exactly one AgentConfig"):
        agent.defn(_WrongInputShape)


# ---------------------------------------------------------------------------
# Direct construction is blocked
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Config resolution + required values (offline unit tests)
# ---------------------------------------------------------------------------


def test_stream_and_approval_policy_default_are_required():
    """``stream`` and ``approval_policy_default`` are required keyword-only constructor
    args, so omitting either is a call-site TypeError — no runtime ``build()`` check to
    forget. The author must make a deliberate safe-by-default approval choice."""
    stream = MagicMock()
    stream.topic.return_value = MagicMock()
    with pytest.raises(TypeError):
        AgentWorkflowRunner(  # type: ignore[call-arg]  — missing stream
            AgentConfig(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
    with pytest.raises(TypeError):
        AgentWorkflowRunner(  # type: ignore[call-arg]  — missing approval_policy_default
            AgentConfig(),
            stream=stream,
        )


def test_approval_policy_resolves_config_over_agent_default(offline_build_policy):
    agent_default = ToolApprovalPolicy.allow_inherently_safe()
    caller_policy = ToolApprovalPolicy.dangerously_skip_all()
    assert (
        offline_build_policy(AgentConfig(), default=agent_default).current_approval_policy
        == agent_default
    )
    assert (
        offline_build_policy(
            AgentConfig(approval_policy=caller_policy), default=agent_default
        ).current_approval_policy
        == caller_policy
    )


def test_set_approval_policy_resolves_matching_pending(offline_build_policy):
    from temporal_agent_harness.harness.agent_workflow import _ApprovalStatus

    runner = offline_build_policy(
        AgentConfig(), default=ToolApprovalPolicy.always_require_approvals()
    )
    runner._status.register_pending_approval(
        "t1", "trusted_tool", {"x": 1}, 1, "turn-1", inherently_safe=False
    )
    runner._status.register_pending_approval(
        "t2", "other_tool", {}, 1, "turn-1", inherently_safe=False
    )

    runner.set_approval_policy(ToolApprovalPolicy.allow_tools(["trusted_tool"]))

    assert runner._status.is_approval_resolved("t1") is True
    entry = runner._status.approval_entry("t1")
    assert entry.status is _ApprovalStatus.APPROVED
    assert entry.reason == "auto-approved by updated policy"
    assert runner._status.is_approval_resolved("t2") is False


def test_custom_fallback_is_consulted_only_as_last_layer(offline_build_policy):
    calls: list[str] = []

    def fallback(ctx) -> bool:
        calls.append(ctx.tool_name)
        return ctx.tool_name == "blessed"

    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.dangerously_skip_all(),
        custom_fallback=fallback,
    )
    assert runner._auto_approves("anything", {}, inherently_safe=False) is True
    assert calls == []

    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.always_require_approvals(),
        custom_fallback=fallback,
    )
    assert runner._auto_approves("blessed", {}, inherently_safe=False) is True
    assert runner._auto_approves("cursed", {}, inherently_safe=False) is False
    assert calls == ["blessed", "cursed"]


def test_protocol_types_use_concrete_annotations():
    """Guard: the wire types must use concrete (not stringized) annotations — they cross
    the Temporal pydantic converter, which builds their TypeAdapter inside the workflow
    sandbox, where a stringized annotation fails to resolve."""
    from temporal_agent_harness.harness.agent_protocol import (
        AcceptedFunction,
        AgentMessage,
        AgentMessageReply,
        AgentStatus,
        MessageContext,
    )

    for cls in (
        AgentMessage,
        AgentStatus,
        AgentMessageReply,
        AcceptedFunction,
        MessageContext,
    ):
        for field_name, annotation in cls.__annotations__.items():
            assert not isinstance(annotation, str), (
                f"{cls.__name__}.{field_name} is a string annotation — "
                f"agent_interface.py must not use `from __future__ import annotations`."
            )


def test_errored_subagent_turn_closes_bracket_on_actual_accepted_turn(offline_build):
    """On an accepted-but-errored child turn, the parent closes the
    [subagent_message_sent … subagent_reply_received] bracket on the child's ACTUAL accepted turn
    number — which the activity threads through the error details — not a re-derived ``expected``.

    Keeps the close-gate key (``workflow_id``, ``subagent_turn``) matching the open marker by
    construction, independent of the validator+enqueue invariant that makes them equal in practice.
    """
    runner = offline_build(AgentConfig())
    # Make a turn active so publish() has a stream context to publish against.
    runner._status.enqueue_message(
        AgentMessage(type="x", payload={}, expected_turn=1), "turn-1"
    )
    runner._status.open_next_turn()
    inst = runner._status.register_subagent("aaaaaa-bbbbbb", "child-wf-1", "k")

    # The activity raises with the child's ACTUAL accepted turn number (7) in the details —
    # deliberately different from the ``expected``/default we pass (2), so the assertion proves we
    # use the threaded value and not ``expected``.
    err = ApplicationError(
        "subagent turn failed",
        {"subagent_turn": 7},
        type="SubagentTurnError",
        non_retryable=True,
    )
    accepted = runner._accepted_turn_from_error(err, default=2)
    assert accepted == 7
    runner._publish_subagent_reply_received(
        inst, "run_script", accepted, outcome="error"
    )

    published = [c.args[0] for c in runner._events.publish.call_args_list]
    replies = [e for e in published if isinstance(e.event, SubagentReplyReceived)]
    assert len(replies) == 1
    rr = replies[0].event
    assert rr.subagent_turn == 7  # the actual accepted turn, NOT the (wrong) expected=2
    assert rr.outcome == "error"
    assert rr.workflow_id == "child-wf-1"
    assert rr.subagent_id == "aaaaaa-bbbbbb"
    # The local turn counter advances off the same accepted turn.
    assert accepted + 1 == 8


def test_accepted_turn_from_error_falls_back_when_detail_absent():
    """If an error carries no ``subagent_turn`` detail (older activity build / unexpected shape),
    the parent falls back to the supplied ``default`` (``expected``) rather than failing."""
    err = ApplicationError("no reply", type="SubagentNoReply", non_retryable=True)
    assert AgentWorkflowRunner._accepted_turn_from_error(err, default=3) == 3


# ---------------------------------------------------------------------------
# Offline build fixtures (workflow APIs __init__ touches are patched out)
# ---------------------------------------------------------------------------


@pytest.fixture
def offline_build(monkeypatch):
    import temporal_agent_harness.harness.agent_workflow as aw

    for handler in ("set_update_handler", "set_query_handler", "set_signal_handler"):
        monkeypatch.setattr(aw.workflow, handler, lambda *a, **k: None)
    monkeypatch.setattr(aw.workflow, "time", lambda: 0.0)
    # The runner generates its short agent_id from workflow.uuid4() in __init__; offline there is
    # no workflow loop, so stub it with a plain uuid.
    monkeypatch.setattr(aw.workflow, "uuid4", lambda: uuid.uuid4())

    def build(config: AgentConfig):
        stream = MagicMock()
        stream.topic.return_value = MagicMock()
        return AgentWorkflowRunner(
            config,
            stream=stream,
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    return build


@pytest.fixture
def offline_build_policy(monkeypatch):
    import temporal_agent_harness.harness.agent_workflow as aw

    for handler in ("set_update_handler", "set_query_handler", "set_signal_handler"):
        monkeypatch.setattr(aw.workflow, handler, lambda *a, **k: None)
    monkeypatch.setattr(aw.workflow, "time", lambda: 0.0)
    # The runner generates its short agent_id from workflow.uuid4() in __init__; offline there is
    # no workflow loop, so stub it with a plain uuid.
    monkeypatch.setattr(aw.workflow, "uuid4", lambda: uuid.uuid4())

    def build(config: AgentConfig, *, default: ToolApprovalPolicy, custom_fallback=None):
        stream = MagicMock()
        stream.topic.return_value = MagicMock()
        return AgentWorkflowRunner(
            config,
            stream=stream,
            approval_policy_default=default,
            custom_approval_fallback=custom_fallback,
        )

    return build


# ---------------------------------------------------------------------------
# Mid-turn dispatch: reject / enqueue / accept, and the refcounted turn bracket
# ---------------------------------------------------------------------------


async def _hold_a_turn_open(client: Client, task_queue: str):
    """Start MidTurnProbeAgent and leave ``work`` running, so a turn is open.

    Returns ``(handle, work_reply)``. Polls status rather than sleeping, so the turn is
    genuinely open before the caller sends its mid-turn message."""
    handle = await _start(client, task_queue, MidTurnProbeAgent)
    work = await _send(handle, "work", {"text": "long"})
    for _ in range(200):
        status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
        if status.turn_active:
            return handle, work
        await asyncio.sleep(0.05)
    raise AssertionError("work never opened a turn")


async def test_reject_handler_fails_while_a_turn_is_open(client_and_queue):
    """A REJECT handler fails the update mid-turn — and the message is never admitted."""
    client, task_queue = client_and_queue
    handle, _work = await _hold_a_turn_open(client, task_queue)

    with pytest.raises(WorkflowUpdateFailedError) as excinfo:
        await _send(handle, "exclusive", {"text": "now"})
    cause = excinfo.value.cause
    assert getattr(cause, "type", None) == "MidTurnRejected"
    assert "exclusive" in str(cause)

    # Rejected at the validator, so nothing was queued and no turn was reserved.
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.pending_turns == []
    assert status.current_turn == 1

    # The same handler succeeds once the agent is idle — REJECT governs only mid-turn arrival.
    await handle.signal(MidTurnProbeAgent.release, "work")
    for _ in range(200):
        status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
        if not status.turn_active:
            break
        await asyncio.sleep(0.05)
    reply = await _send(handle, "exclusive", {"text": "later"})
    assert reply.turn_number == 2


async def test_accept_handler_joins_the_open_turn(client_and_queue):
    """An ACCEPT message shares the open turn's id/number instead of getting its own.

    And the bracket stays singular: ONE turn_started and ONE turn_end for that turn, with
    both participants' replies inside it."""
    client, task_queue = client_and_queue
    handle, work = await _hold_a_turn_open(client, task_queue)

    steer = await _send(handle, "steer", {"text": "ride along"})
    # Same turn — not a new one, and not queued behind the open one.
    assert steer.turn_id == work.turn_id
    assert steer.turn_number == work.turn_number
    assert steer.pending is False

    # Nothing was queued — a join does not take a queue slot. (We deliberately do NOT assert
    # turn_participants == 2 here: `steer` returns immediately, so the count may already be
    # back to 1. That IS the point of ACCEPT — it ran now rather than waiting. The blocking
    # `outlive` handler is where the count is observable; see the sibling test below.)
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.turn_active is True
    assert status.pending_turns == []

    # A JOIN CONSUMES NO TURN SLOT. This is the invariant every client's expected_turn
    # bookkeeping rests on: after joining, the agent still hands out the SAME next turn
    # number, so a caller that increments once per message *sent* over-counts here and gets
    # StaleTurn on everything after. reply.turn_number + 1 is the reliable source.
    assert await _next_expected_turn(handle) == steer.turn_number + 1
    again = await _send(handle, "steer", {"text": "and again"})
    assert again.turn_id == work.turn_id
    assert await _next_expected_turn(handle) == steer.turn_number + 1

    await handle.signal(MidTurnProbeAgent.release, "work")
    events = await _collect_until_turn_end(client, handle.id)

    ours = [e for e in events if e.turn_id == work.turn_id]
    types = [e.event.type for e in ours]
    assert types.count(AgentEventType.TURN_STARTED) == 1
    assert types.count(AgentEventType.TURN_END) == 1
    assert types[-1] == AgentEventType.TURN_END
    # Both participants replied inside the one bracket.
    replies = [e.event.output for e in ours if e.event.type == AgentEventType.REPLY]
    assert len(replies) == 3  # the opener plus both joins
    assert {r["text"].split(":")[0] for r in replies} == {"worked", "steer"}


async def test_message_context_distinguishes_joining_from_opening(client_and_queue):
    """``MessageContext.joined_turn`` tells the handler which case it is in.

    Same handler, both cases: idle arrival opens a turn (False), mid-turn arrival joins
    one (True). This is what lets one text box start a prompt when idle and steer when busy."""
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, MidTurnProbeAgent)

    # Idle: opens its own turn.
    idle = await _send(handle, "steer", {"text": "fresh"})
    for _ in range(200):
        if await handle.query("joined", result_type=list[bool]):
            break
        await asyncio.sleep(0.05)
    assert await handle.query("joined", result_type=list[bool]) == [False]
    assert idle.pending is False

    # Mid-turn: joins the open turn.
    _handle2, work = await _hold_a_turn_open(client, task_queue)
    joined_reply = await _send(_handle2, "steer", {"text": "ride"})
    assert joined_reply.turn_id == work.turn_id
    for _ in range(200):
        if await _handle2.query("joined", result_type=list[bool]):
            break
        await asyncio.sleep(0.05)
    assert await _handle2.query("joined", result_type=list[bool]) == [True]

    await _handle2.signal(MidTurnProbeAgent.release, "work")


async def test_joined_handler_keeps_its_turn_after_a_sibling_finishes(client_and_queue):
    """The turn id must survive the FIRST participant finishing, not just the last.

    Regression guard for the loudest way to get refcounting wrong: clearing the turn id on
    first completion makes a still-running joined handler's next publish (or gated tool call)
    hard-raise, because the runner reports no active turn."""
    client, task_queue = client_and_queue
    handle, work = await _hold_a_turn_open(client, task_queue)

    joined = await _send(handle, "outlive", {"text": "after you"})
    assert joined.turn_id == work.turn_id

    # Both participants are in flight, sharing one turn — the count is observable here
    # because `outlive` parks rather than returning immediately.
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.turn_participants == 2
    assert status.pending_turns == []

    # Let the OPENER finish first, while the joined handler is still parked.
    await handle.signal(MidTurnProbeAgent.release, "work")
    for _ in range(200):
        status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
        if status.turn_participants == 1:
            break
        await asyncio.sleep(0.05)
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    # One participant left, so the turn is STILL open — turn_end has not been claimed.
    assert status.turn_participants == 1
    assert status.turn_active is True

    # Now the joined handler publishes against the turn. This is the assertion that fails
    # loudly if the turn id was cleared early.
    await handle.signal(MidTurnProbeAgent.release, "outlive")
    events = await _collect_until_turn_end(client, handle.id)
    assert await handle.query("published_after_sibling", result_type=bool) is True

    ours = [e for e in events if e.turn_id == work.turn_id]
    assert [e.event.type for e in ours].count(AgentEventType.TURN_END) == 1
    assert [e.event.type for e in ours][-1] == AgentEventType.TURN_END


async def test_close_drains_an_in_flight_joined_handler(client_and_queue):
    """Closing must not discard a joined handler's work.

    ``_closed`` is only observed between loop iterations, so without an explicit drain the
    workflow would complete while a spawned participant was still parked — Temporal would
    warn and the result would be lost."""
    client, task_queue = client_and_queue
    handle, work = await _hold_a_turn_open(client, task_queue)
    await _send(handle, "outlive", {"text": "drain me"})
    await handle.signal(MidTurnProbeAgent.release, "work")

    # Close while the joined handler is still parked.
    await handle.signal("close")
    await asyncio.sleep(0.2)
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.turn_active is True, "closed before the in-flight join finished"

    # Releasing it lets the workflow wind down — and its work was not thrown away.
    await handle.signal(MidTurnProbeAgent.release, "outlive")
    await handle.result()
    assert await handle.query("published_after_sibling", result_type=bool) is True


async def test_agent_interface_reports_mid_turn_and_model_callable(client_and_queue):
    """Discovery carries the dispatch metadata a generic client needs, for EVERY handler.

    A UI reads ``mid_turn`` to tell the user whether sending will queue, join, or be
    rejected; a parent reads ``model_callable`` to honor the author's intent by default."""
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, MidTurnProbeAgent)

    functions = await handle.query(
        AGENT_INTERFACE_QUERY, result_type=list[AcceptedFunction]
    )
    by_name = {f.name: f for f in functions}
    assert set(by_name) == {"work", "steer", "outlive", "exclusive"}
    assert by_name["work"].mid_turn is MidTurn.ENQUEUE
    assert by_name["steer"].mid_turn is MidTurn.ACCEPT
    assert by_name["exclusive"].mid_turn is MidTurn.REJECT
    assert all(f.model_callable for f in functions)
    # The injected MessageContext is workflow-supplied, so it must not leak into the schema
    # a caller (or a model) fills in.
    assert set(by_name["steer"].parameters["properties"]) == {"text"}


async def test_stale_expected_turn_is_rejected(client_and_queue):
    """``expected_turn`` is a staleness token, and the workflow enforces it."""
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    with pytest.raises(WorkflowUpdateFailedError) as excinfo:
        await handle.execute_update(
            SEND_AGENT_MESSAGE_UPDATE,
            AgentMessage(type="greet", payload={"name": "Ada"}, expected_turn=99),
            result_type=AgentMessageReply,
        )
    cause = excinfo.value.cause
    assert getattr(cause, "type", None) == "StaleTurn"

    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.current_turn == 0 and status.pending_turns == []
