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
from pydantic import ValidationError
import pytest_asyncio
from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowHandle,
    WorkflowUpdateFailedError,
)
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
    AgentStatus,
    AutoApprovalVerdict,
    AutoApprovalCriteria,
    AutoApprovalCriteriaSet,
    AutoApprovalDecision,
    MessageContext,
    MessageDisposition,
    MessageHandlerEnd,
    MidTurn,
    SubagentReplyReceived,
    TextMessage,
    TextReply,
    AutoApprovalContext,
    ToolApprovalPolicy,
    AgentMessageReply,
)
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_client import AgentClient, JoinedTurnError
from temporal_agent_harness.harness.agent_workflow import (
    AUTO_MODE_EVALUATOR_ATTR,
    Injected,
    _Admission,
    _discover_handlers,
    _evaluator_label,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext


def _ctx(tool_name: str, **kwargs) -> AutoApprovalContext:
    """A minimal gated-call context for exercising an auto mode evaluator offline.

    Carries a governing criteria set because every context does: the gate resolves one
    before it will call an evaluator, so a context without one is not a state the harness
    can produce (see ``test_an_ungoverned_call_never_reaches_an_evaluator``)."""
    return AutoApprovalContext(
        tool_name=tool_name,
        tool_input=kwargs.pop("tool_input", {}),
        inherently_safe=kwargs.pop("inherently_safe", False),
        criteria_set=kwargs.pop(
            "criteria_set", AutoApprovalCriteriaSet(effect="Does something.")
        ),
        criteria_set_name=kwargs.pop("criteria_set_name", "default"),
        **kwargs,
    )


async def _run_evaluator(runner, tool_name: str, **kwargs):
    """Run the runner's auto mode evaluator offline.

    Only the "no evaluator wired" short-circuit is reachable here: once there IS one it runs
    as a cancellable task raced against the gate on the workflow event loop, which does not
    exist offline. Everything past that point is asserted end-to-end in
    test_tool_approvals.py, against a real workflow and a real stream."""
    return await runner._run_auto_mode_evaluator(
        _ctx(tool_name, **kwargs),
        tool_id=f"call-{tool_name}",
        stream=TurnStreamContext(turn_id="turn-1", turn_number=1, agent_id="a1b2c3"),
    )

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
        """Always raises — to prove an errored turn publishes message_handler_error + turn_end and
        the loop survives for the next message."""
        raise RuntimeError(f"boom: {message.text}")

    @workflow.query
    def seen(self) -> list[str]:
        return self._seen


@agent.defn
class NoEvaluatorProbeAgent:
    """No ``auto_mode_evaluator`` wired, and a handler that tries to switch auto mode on
    anyway — to prove the rejection fails that ONE message, not the session."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_tools(["get_order"]),
        )
        self._seen: list[str] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts(mid_turn=MidTurn.ENQUEUE, model_callable=False)
    async def enable_auto_mode(self, message: TextMessage) -> TextReply:
        """Try to hand gated calls to an evaluator this agent does not have."""
        self._runner.set_approval_policy(self._runner.approval_policy.with_auto_mode(True))
        return TextReply(text="unreachable")

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def greet(self, message: Greeting) -> Greeted:
        """Greet a person by name."""
        self._seen.append(f"greet:{message.name}")
        return Greeted(message=f"hi {message.name}")

    @workflow.query
    def seen(self) -> list[str]:
        return self._seen


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
        self._runner.publish(MessageHandlerEnd(output={"note": "still in the turn"}))
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
        workflows=[TypedProbeAgent, MidTurnProbeAgent, NoEvaluatorProbeAgent],
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


async def _send(
    handle: WorkflowHandle,
    type: str,
    payload: dict[str, Any],
    *,
    update_id: str | None = None,
) -> AgentMessageReply:
    return await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type=type, payload=payload),
        id=update_id,
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


def _subscribe_all(client: Client, workflow_id: str):
    """Subscribe to a workflow's whole event log from the start (live-tailing)."""
    from datetime import timedelta

    from temporalio.contrib.workflow_streams import WorkflowStreamClient

    return WorkflowStreamClient.create(client, workflow_id).subscribe(
        topics=["turn_events"],
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    )


async def _collect_until_turn_end(client: Client, workflow_id: str) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe_all(client, workflow_id):
            events.append(item.data)
            if item.data.event.type == AgentEventType.TURN_END:
                break
    return events


async def _collect_events(client: Client, workflow_id: str) -> list[AgentEvent]:
    """Every event published SO FAR, without waiting for anything more.

    ``subscribe`` live-tails, so it never ends on its own; the short timeout is the read —
    for asserting what has NOT been published yet, which no terminal event can signal."""
    events: list[AgentEvent] = []
    try:
        async with asyncio.timeout(2):
            async for item in _subscribe_all(client, workflow_id):
                events.append(item.data)
    except TimeoutError:
        pass
    return events


def _reply_text(events: list[AgentEvent]) -> str:
    reply = next(
        e.event for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END
    )
    text = reply.output.get("text")
    assert isinstance(text, str)
    return text


async def test_reply_is_the_handler_return_value(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)
    await _send(handle, "greet", {"name": "Ada"})

    events = await _collect_until_turn_end(client, handle.id)
    reply = next(
        e.event for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END
    )
    # The reply carries the handler's return model serialized to a dict.
    assert reply.output == {"message": "hi Ada"}


async def test_handler_error_publishes_message_handler_error_and_loop_survives(
    client_and_queue,
):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    # A raising handler → message_handler_error (then turn_end), and the session stays alive.
    await _send(handle, "boom", {"text": "x"})
    events = await _collect_until_turn_end(client, handle.id)
    errors = [
        e for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_ERROR
    ]
    assert len(errors) == 1 and "boom: x" in errors[0].event.message
    # The next message is still handled normally.
    await _send(handle, "greet", {"name": "Bob"})
    seen = await _wait_for_seen(handle, 1)
    assert "greet:Bob" in seen


async def test_a_rejected_auto_mode_policy_fails_the_message_not_the_workflow(
    client_and_queue,
):
    """The rejection is a non-retryable ApplicationError, but that flag only governs retries.
    Raised inside a message handler it is caught like any handler failure: that message gets
    ``message_handler_error``, the live policy is untouched, and the agent keeps serving."""
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, NoEvaluatorProbeAgent)

    await _send(handle, "enable_auto_mode", {"text": "please"})
    events = await _collect_until_turn_end(client, handle.id)
    errors = [e for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_ERROR]
    assert len(errors) == 1
    assert "auto_mode_enabled=True" in errors[0].event.message

    # Rejected BEFORE anything changed: status still reports the original posture.
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.approval_policy == ToolApprovalPolicy.allow_tools(["get_order"])
    assert status.approval_policy.auto_mode_enabled is False

    # And the session is alive: still running, and the next message is handled normally.
    assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
    await _send(handle, "greet", {"name": "Bob"})
    assert "greet:Bob" in await _wait_for_seen(handle, 1)


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


class _NamedShape:
    @workflow.run
    async def run(self, config: AgentConfig) -> None: ...


class _BareShape:
    @workflow.run
    async def run(self, config: AgentConfig) -> None: ...


class _StackedShape:
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


def test_agent_defn_registers_the_workflow_with_forwarded_options():
    agent.defn(
        name="NamedProbeAgent",
        workflow_options=agent.WorkflowDefnOptions(sandboxed=False),
    )(_NamedShape)
    defn = workflow._Definition.must_from_class(_NamedShape)
    assert defn.name == "NamedProbeAgent"
    assert defn.sandboxed is False


def test_agent_defn_defaults_workflow_name_to_class_name():
    agent.defn(_BareShape)
    assert workflow._Definition.must_from_class(_BareShape).name == "_BareShape"


def test_agent_defn_cannot_be_stacked_with_workflow_defn():
    with pytest.raises(ValueError, match="already contains workflow definition"):
        workflow.defn(agent.defn(_StackedShape))


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
        AgentConfig(), default=ToolApprovalPolicy.always_require_human_approval()
    )
    runner._status.register_pending_approval(
        "t1", "trusted_tool", {"x": 1}, 1, "turn-1", "msg-1", inherently_safe=False
    )
    runner._status.register_pending_approval(
        "t2", "other_tool", {}, 1, "turn-1", "msg-1", inherently_safe=False
    )

    runner.set_approval_policy(ToolApprovalPolicy.allow_tools(["trusted_tool"]))

    assert runner._status.is_approval_resolved("t1") is True
    entry = runner._status.approval_entry("t1")
    assert entry.status is _ApprovalStatus.APPROVED
    assert entry.reason == "auto-approved by updated policy"
    assert runner._status.is_approval_resolved("t2") is False


def test_an_auto_mode_evaluator_must_be_async(offline_build_policy):
    """Rejected at CONSTRUCTION — inside the agent's @workflow.init — so the error lands
    next to the developer's own wiring instead of inside the first gated tool call, which
    in a real agent might not happen until production.

    Not a style rule: the gate races the evaluator against a human decision and cancels
    whichever loses, and only a coroutine can be cancelled."""

    def sync_evaluator(ctx):
        return AutoApprovalDecision(AutoApprovalVerdict.APPROVE)

    with pytest.raises(TypeError) as excinfo:
        offline_build_policy(
            AgentConfig(),
            default=ToolApprovalPolicy.always_require_human_approval(),
            auto_mode_evaluator=sync_evaluator,
        )
    assert "must be an async function" in str(excinfo.value)
    assert "sync_evaluator" in str(excinfo.value)


def test_a_callable_object_with_an_async_call_is_a_valid_evaluator(offline_build_policy):
    """An evaluator that carries state is a normal thing to want, and rejecting it would
    only push authors back to a bare function plus a global."""

    class Evaluator:
        async def __call__(self, ctx):
            return AutoApprovalDecision(AutoApprovalVerdict.APPROVE)

    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.always_require_human_approval(),
        auto_mode_evaluator=Evaluator(),
    )
    assert runner.current_status.has_auto_approval_evaluator is True


async def test_no_evaluator_wired_means_no_decision(offline_build_policy):
    runner = offline_build_policy(
        AgentConfig(), default=ToolApprovalPolicy.always_require_human_approval()
    )
    assert await _run_evaluator(runner, "x") is None


# ---------------------------------------------------------------------------
# Auto mode is a POLICY MODE, not a fallback that activates because an evaluator exists
# ---------------------------------------------------------------------------


async def _evaluator(ctx):  # pragma: no cover - reached only where noted
    return AutoApprovalDecision(AutoApprovalVerdict.APPROVE)


def test_auto_mode_is_off_by_default_even_with_an_evaluator_wired(offline_build_policy):
    """The posture an operator asked for: explicit static approvals, and a human on
    everything else. Wiring an evaluator must not quietly opt them into a machine
    deciding."""
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.always_require_human_approval(),
        auto_mode_evaluator=_evaluator,
    )
    assert runner._policy_consults_auto_mode("anything", inherently_safe=False) is False
    # Still reported as wired — a client needs to know the agent HAS one to offer the toggle.
    assert runner.current_status.has_auto_approval_evaluator is True


def test_auto_mode_on_consults_the_evaluator_for_a_gated_call(offline_build_policy):
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.auto_mode(),
        auto_mode_evaluator=_evaluator,
    )
    assert runner._policy_consults_auto_mode("run_sql", inherently_safe=False) is True


def test_auto_mode_on_with_no_evaluator_wired_is_rejected(offline_build_policy):
    """A switch with nothing behind it is refused, on every route a policy is installed by.

    Tolerating it would misrepresent the posture: status would report auto mode ON while
    no call was ever judged. And it is not only the author's to get wrong — a caller's
    ``AgentConfig`` or a handler's runtime ``set_approval_policy`` can ask for it too."""
    # The agent's own default.
    with pytest.raises(ApplicationError) as err:
        offline_build_policy(AgentConfig(), default=ToolApprovalPolicy.auto_mode())
    assert err.value.type == "AutoModeWithoutEvaluator"
    assert err.value.non_retryable

    # A caller's per-session override, against an agent that wired no evaluator.
    with pytest.raises(ApplicationError):
        offline_build_policy(
            AgentConfig(approval_policy=ToolApprovalPolicy.auto_mode()),
            default=ToolApprovalPolicy.always_require_human_approval(),
        )

    # A runtime update — rejected before anything changes, so the live policy is intact.
    runner = offline_build_policy(
        AgentConfig(), default=ToolApprovalPolicy.allow_tools(["get_order"])
    )
    with pytest.raises(ApplicationError):
        runner.set_approval_policy(runner.approval_policy.with_auto_mode(True))
    assert runner.approval_policy == ToolApprovalPolicy.allow_tools(["get_order"])

    # The opposite combination stays legal: an evaluator wired, switched on later.
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.always_require_human_approval(),
        auto_mode_evaluator=_evaluator,
    )
    runner.set_approval_policy(runner.approval_policy.with_auto_mode(True))
    assert runner.approval_policy.auto_mode_enabled is True


def test_a_call_the_allow_list_approves_never_reaches_the_evaluator(offline_build_policy):
    """Auto mode sits BELOW the allow-list layers, so it cannot widen them and never costs
    a model call for something already approved. This is also what makes an explicit
    "approve & remember" permanently outrank the machine."""
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.auto_mode(pre_approved_tools=["get_order"]),
        auto_mode_evaluator=_evaluator,
    )
    assert runner._policy_consults_auto_mode("get_order", inherently_safe=False) is False
    assert runner._policy_consults_auto_mode("run_sql", inherently_safe=False) is True


def test_remembering_a_tool_takes_it_out_of_auto_modes_hands(offline_build_policy):
    """A human saying "always allow this" is a stronger statement than any verdict a model
    could reach, so the allow-list layer swallows the call from then on."""
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.auto_mode(),
        auto_mode_evaluator=_evaluator,
    )
    assert runner._policy_consults_auto_mode("run_sql", inherently_safe=False) is True
    runner.set_approval_policy(runner.approval_policy.with_tool_allowed("run_sql"))
    assert runner._policy_consults_auto_mode("run_sql", inherently_safe=False) is False


def test_auto_mode_toggles_at_runtime_without_losing_the_allow_list(offline_build_policy):
    """The runtime path a message handler takes. Turning the machine off must not discard
    the explicit approvals a user has built up, which is why the toggle is a modification
    of the live policy rather than a fresh one."""
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.auto_mode(pre_approved_tools=["get_order"]),
        auto_mode_evaluator=_evaluator,
    )
    runner.set_approval_policy(runner.approval_policy.with_auto_mode(False))

    assert runner._policy_consults_auto_mode("run_sql", inherently_safe=False) is False
    assert runner.approval_policy.auto_approve_tools == frozenset({"get_order"})
    assert runner.current_status.approval_policy.auto_mode_enabled is False

    runner.set_approval_policy(runner.approval_policy.with_auto_mode(True))
    assert runner._policy_consults_auto_mode("run_sql", inherently_safe=False) is True


def test_auto_mode_over_dangerously_skip_all_is_unrepresentable():
    """Layer 0 approves everything, so there is nothing left for a machine to decide — and the
    POLICY refuses to hold that combination rather than leaving each caller to notice.

    A policy claiming auto mode while nothing is gated is worse than an error: it reads as a
    guardrail that is not there. Closed on every route, including ``model_validate`` (the wire
    path a caller's ``AgentConfig.approval_policy`` arrives by) and the value-like helpers,
    which reconstruct rather than ``model_copy`` precisely because pydantic skips validators on
    a copy."""
    with pytest.raises(ValidationError):
        ToolApprovalPolicy(dangerously_skip_all_approvals=True, auto_mode_enabled=True)
    with pytest.raises(ValidationError):
        ToolApprovalPolicy.dangerously_skip_all().with_auto_mode(True)
    with pytest.raises(ValidationError):
        ToolApprovalPolicy.model_validate(
            {"dangerously_skip_all_approvals": True, "auto_mode_enabled": True}
        )

    # Either alone is fine, and the helpers still compose.
    assert ToolApprovalPolicy.dangerously_skip_all().auto_mode_enabled is False
    assert ToolApprovalPolicy.auto_mode().with_tool_allowed("x").auto_mode_enabled is True
    assert (
        ToolApprovalPolicy.auto_mode().with_auto_mode(False).with_auto_mode(True).auto_mode_enabled
        is True
    )


def test_with_changes_reaches_every_field_and_re_validates():
    """The public, validated way to derive a policy — so nobody has to reach for
    ``model_copy``, which skips the cross-field validator.

    Covering EVERY field matters: a partial set of single-field helpers is what sends
    someone to ``model_copy`` for the one field that was left out, with no warning that it
    punches through the invariant. ``None`` means leave-as-is, because a policy is edited
    against a live value whose ``auto_approve_tools`` grows every time a human answers a
    gate with "approve and stop asking"."""
    policy = ToolApprovalPolicy.auto_mode(pre_approved_tools=["a"])

    # Each field is reachable, and an unnamed field is left alone.
    assert policy.with_changes(auto_approve_inherently_safe=True) == ToolApprovalPolicy(
        auto_approve_tools=frozenset({"a"}),
        auto_approve_inherently_safe=True,
        auto_mode_enabled=True,
    )
    assert policy.with_changes(auto_approve_tools=["b", "c"]).auto_approve_tools == frozenset(
        {"b", "c"}
    )
    assert policy.with_changes(auto_approve_tools=()).auto_approve_tools == frozenset()
    assert policy.with_changes(auto_mode_enabled=False).auto_approve_tools == frozenset({"a"})
    assert policy.with_changes().with_changes() == policy

    # And it re-validates, which is the whole reason it exists.
    with pytest.raises(ValidationError):
        policy.with_changes(dangerously_skip_all_approvals=True)
    assert (
        ToolApprovalPolicy.dangerously_skip_all()
        .with_changes(dangerously_skip_all_approvals=False, auto_mode_enabled=True)
        .auto_mode_enabled
        is True
    )


def test_a_call_layer_zero_approves_never_reaches_auto_mode(offline_build_policy):
    """The layer-0 bypass still holds for the reachable case: ``dangerously_skip_all`` with auto
    mode off approves outright, so no evaluation is even considered."""
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.dangerously_skip_all(),
        auto_mode_evaluator=_evaluator,
    )
    assert runner._policy_consults_auto_mode("run_sql", inherently_safe=False) is False


# ---------------------------------------------------------------------------
# The HARNESS guarantees an evaluator is only ever called for a governed call
# ---------------------------------------------------------------------------


def _auto_ctx(runner, tool_name="run_sql", declared=None):
    return runner._auto_mode_context(
        tool_name,
        {"sql": "DELETE FROM orders"},
        inherently_safe=False,
        tool_description="Run a SQL statement.",
        declared_criteria_set=declared,
    )


_GOVERNED = AutoApprovalCriteria(
    sets={"destructive": AutoApprovalCriteriaSet(effect="Destroys data.")},
    default="destructive",
)


def test_a_governed_call_gets_a_context_carrying_its_resolved_rules(offline_build_policy):
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.auto_mode(),
        auto_approval_criteria=_GOVERNED,
        auto_mode_evaluator=_evaluator,
    )
    ctx = _auto_ctx(runner)

    assert ctx is not None
    # Non-optional on the context, which is the whole point: an evaluator cannot be handed
    # a call with no rules, so it needs no defensive branch and cannot forget one.
    assert ctx.criteria_set.effect == "Destroys data."
    assert ctx.criteria_set_name == "destructive"
    assert ctx.criteria_version == 0


@pytest.mark.parametrize(
    "criteria",
    [
        pytest.param(AutoApprovalCriteria(), id="nothing configured at all"),
        pytest.param(
            AutoApprovalCriteria(tools={"run_sql": "typo"}, default="cautious"),
            id="assigned set is not registered",
        ),
        pytest.param(
            AutoApprovalCriteria(
                sets={"empty": AutoApprovalCriteriaSet(min_confidence=0.99)},
                default="empty",
            ),
            id="set holds only a threshold, so no rule to judge against",
        ),
    ],
)
def test_an_ungoverned_call_never_reaches_an_evaluator(offline_build_policy, criteria):
    """THE GUARANTEE. Auto mode is on and an evaluator is wired, but no criteria set governs
    this tool — so the harness refuses to build a context at all and the call falls through
    to the human gate.

    Enforced here rather than in each evaluator on purpose. "Nobody wrote rules for this
    tool" is not a judgment call, and leaving it to an implementation would make a safety
    property of auto mode depend on every evaluator author remembering it — while handing a
    model an empty rulebook, which is the input most likely to come back a confident
    approve."""
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.auto_mode(),
        auto_approval_criteria=criteria,
        auto_mode_evaluator=_evaluator,
    )
    assert _auto_ctx(runner) is None


def test_a_tools_declared_set_can_be_what_governs_it(offline_build_policy):
    """The decorator's name is a real resolution source, not decoration: with nothing else
    configured it is what makes the call governed at all."""
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.auto_mode(),
        auto_approval_criteria=AutoApprovalCriteria(
            sets={"financial": AutoApprovalCriteriaSet(effect="Moves money.")}
        ),
        auto_mode_evaluator=_evaluator,
    )
    assert _auto_ctx(runner, declared=None) is None
    ctx = _auto_ctx(runner, declared="financial")
    assert ctx is not None and ctx.criteria_set_name == "financial"


def test_auto_mode_off_builds_no_context_however_good_the_criteria(offline_build_policy):
    """The switch is checked before the rules, so a well-configured rulebook cannot smuggle
    a model into a posture that asked for a human."""
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.always_require_human_approval(),
        auto_approval_criteria=_GOVERNED,
        auto_mode_evaluator=_evaluator,
    )
    assert _auto_ctx(runner) is None


def test_the_criteria_requirement_applies_to_a_CUSTOM_evaluator_too(offline_build_policy):
    """Not a Jev-specific courtesy. Criteria are the schema EVERY evaluator reads, so a
    hand-rolled one is equally never handed an ungoverned call — even one that would have
    decided on tool name alone and never looked at the rules.

    The guarantee is about what an evaluator may be ASKED, not about what it must use: an
    implementation is free to ignore the rules it is given, but it can never be invoked for a
    tool the operator never brought under auto mode."""
    seen: list[str] = []

    async def decides_on_name_alone(ctx):  # pragma: no cover - must never be reached
        seen.append(ctx.tool_name)
        return AutoApprovalDecision(AutoApprovalVerdict.APPROVE)

    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.auto_mode(),
        auto_mode_evaluator=decides_on_name_alone,
    )
    assert _auto_ctx(runner) is None
    assert seen == []


def test_a_runtime_criteria_swap_can_bring_a_tool_under_auto_mode(offline_build_policy):
    """The end-user path: a call that had no rules, and so went to a person, starts being
    decided once the user configures rules for it — without a redeploy."""
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.auto_mode(),
        auto_mode_evaluator=_evaluator,
    )
    assert _auto_ctx(runner) is None

    runner.set_auto_approval_criteria(_GOVERNED)
    ctx = _auto_ctx(runner)
    assert ctx is not None
    # And the verdict it produces will name the generation of rules that governed it.
    assert ctx.criteria_version == 1


# ---------------------------------------------------------------------------
# Criteria: resolution, the config override, and runtime updates
# ---------------------------------------------------------------------------


def test_the_callers_criteria_override_the_agents_default(offline_build_policy):
    """Same resolution as ``approval_policy``, and for the same reason: the posture usually
    belongs to the end user of the product, who must be able to start a session under their
    own rules — including redefining a set the agent's own tools point at."""
    agents_own = AutoApprovalCriteria(
        sets={"financial": AutoApprovalCriteriaSet(effect="the agent's own wording")}
    )
    callers = AutoApprovalCriteria(
        sets={"financial": AutoApprovalCriteriaSet(effect="the caller's wording")}
    )
    runner = offline_build_policy(
        AgentConfig(auto_approval_criteria=callers),
        default=ToolApprovalPolicy.auto_mode(),
        auto_mode_evaluator=_evaluator,
        auto_approval_criteria=agents_own,
    )
    assert runner.auto_approval_criteria.sets["financial"].effect == "the caller's wording"


def test_criteria_default_to_empty_which_means_ask_a_person(offline_build_policy):
    runner = offline_build_policy(
        AgentConfig(), default=ToolApprovalPolicy.auto_mode(), auto_mode_evaluator=_evaluator
    )
    assert runner.auto_approval_criteria == AutoApprovalCriteria()
    assert runner.auto_approval_criteria.for_tool("anything") is None


def test_swapping_criteria_at_runtime_bumps_the_version(offline_build_policy):
    """The version is what lets a stored verdict name the generation of rules that produced
    it, now that the rules can change under an agent mid-session."""
    runner = offline_build_policy(
        AgentConfig(), default=ToolApprovalPolicy.auto_mode(), auto_mode_evaluator=_evaluator
    )
    assert runner._status.auto_approval_criteria_version == 0

    runner.set_auto_approval_criteria(
        AutoApprovalCriteria(sets={"read_only": AutoApprovalCriteriaSet(effect="Reads.")})
    )
    assert runner._status.auto_approval_criteria_version == 1
    assert runner.auto_approval_criteria.sets["read_only"].effect == "Reads."


def test_assign_tool_criteria_retargets_one_tool_without_touching_the_bodies(
    offline_build_policy,
):
    """The call a message handler usually wants, and it works for tools the agent author
    never wrote, since assignment is by name."""
    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.auto_mode(),
        auto_mode_evaluator=_evaluator,
        auto_approval_criteria=AutoApprovalCriteria(
            sets={
                "cautious": AutoApprovalCriteriaSet(effect="Unknown."),
                "read_only": AutoApprovalCriteriaSet(effect="Reads."),
            },
            default="cautious",
        ),
    )
    runner.assign_tool_criteria("some_mcp_tool", "read_only")

    criteria = runner.auto_approval_criteria
    assert criteria.for_tool("some_mcp_tool").effect == "Reads."
    # Untouched bodies, and every other tool still on the catch-all.
    assert criteria.for_tool("anything_else").effect == "Unknown."
    assert set(criteria.sets) == {"cautious", "read_only"}


def test_the_live_criteria_are_not_surfaced_on_status(offline_build_policy):
    """Deliberate: a tool's DECLARED set lives in a decorator and the runner holds no tool
    registry, so any per-tool map published here would be silently incomplete for exactly
    the tools whose author already picked one. What a client needs — whether the machine is
    deciding at all — rides on the policy instead."""
    runner = offline_build_policy(
        AgentConfig(), default=ToolApprovalPolicy.auto_mode(), auto_mode_evaluator=_evaluator
    )
    status = runner.current_status
    assert not hasattr(status, "auto_approval_criteria")
    assert status.approval_policy.auto_mode_enabled is True


def test_the_evaluator_label_prefers_the_stamped_name(offline_build_policy):
    """Published as `evaluator` on all three of an evaluation's events. Read off the
    CALLABLE, because the started and error events have no decision to read it from."""

    async def plain(ctx):  # pragma: no cover - never invoked
        return AutoApprovalDecision(AutoApprovalVerdict.APPROVE)

    async def stamped(ctx):  # pragma: no cover - never invoked
        return AutoApprovalDecision(AutoApprovalVerdict.APPROVE)

    setattr(stamped, AUTO_MODE_EVALUATOR_ATTR, "jev_evaluator")

    assert _evaluator_label(stamped) == "jev_evaluator"
    assert _evaluator_label(plain).endswith("plain")


def test_policy_update_cascade_does_not_consult_the_evaluator(offline_build_policy):
    """A relaxing policy update re-evaluates the POLICY against pending calls, never the
    evaluator: each pending entry already escalated once, and for an AI evaluator re-asking
    would mean paying for another model call per pending call on every policy change."""
    calls: list[str] = []

    async def evaluator(ctx) -> AutoApprovalDecision:
        calls.append(ctx.tool_name)
        return AutoApprovalDecision(AutoApprovalVerdict.APPROVE)

    runner = offline_build_policy(
        AgentConfig(),
        default=ToolApprovalPolicy.always_require_human_approval(),
        auto_mode_evaluator=evaluator,
    )
    runner._status.register_pending_approval(
        "t1", "trusted_tool", {}, 1, "turn-1", "msg-1", inherently_safe=False
    )
    runner._status.register_pending_approval(
        "t2", "other_tool", {}, 1, "turn-1", "msg-1", inherently_safe=False
    )

    runner.set_approval_policy(ToolApprovalPolicy.allow_tools(["trusted_tool"]))

    assert calls == []
    # Only the call the NEW POLICY covers was released; the other still needs a decision.
    assert runner._status.is_approval_resolved("t1") is True
    assert runner._status.is_approval_resolved("t2") is False


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
    number, which the activity threads through the error details. The parent keeps no turn
    counter of its own, so this is the only source, and it keeps the close-gate key
    (``workflow_id``, ``subagent_turn``) matching the open marker by construction.
    """
    runner = offline_build(AgentConfig())
    # Make a turn active so publish() has a stream context to publish against.
    runner._status.enqueue_message(
        _Admission(
            message=AgentMessage(type="x", payload={}),
            turn_id="turn-1",
            turn_number=1,
            message_id="msg-1",
        )
    )
    runner._status.open_next_turn()
    inst = runner._status.register_subagent("aaaaaa-bbbbbb", "child-wf-1", "k")

    # The activity raises with the child's ACTUAL accepted turn number (7) in the details.
    err = ApplicationError(
        "subagent turn failed",
        {"subagent_turn": 7},
        type="SubagentTurnError",
        non_retryable=True,
    )
    accepted = runner._accepted_turn_from_error(err)
    assert accepted == 7
    runner._publish_subagent_reply_received(
        inst, "run_script", accepted, outcome="error"
    )

    published = [c.args[0] for c in runner._events.publish.call_args_list]
    replies = [e for e in published if isinstance(e.event, SubagentReplyReceived)]
    assert len(replies) == 1
    rr = replies[0].event
    assert rr.subagent_turn == 7
    assert rr.outcome == "error"
    assert rr.workflow_id == "child-wf-1"
    assert rr.subagent_id == "aaaaaa-bbbbbb"


def test_accepted_turn_from_error_raises_when_detail_absent():
    """An accepted-but-errored turn with no ``subagent_turn`` detail is a broken activity
    contract. The parent raises loudly rather than closing the bracket on an invented number
    that the client merge's close gate would never match."""
    err = ApplicationError("no reply", type="SubagentNoReply", non_retryable=True)
    with pytest.raises(ApplicationError) as excinfo:
        AgentWorkflowRunner._accepted_turn_from_error(err)
    assert excinfo.value.type == "SubagentProtocolError"


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

    def build(
        config: AgentConfig,
        *,
        default: ToolApprovalPolicy,
        auto_mode_evaluator=None,
        auto_approval_criteria=None,
    ):
        stream = MagicMock()
        stream.topic.return_value = MagicMock()
        return AgentWorkflowRunner(
            config,
            stream=stream,
            approval_policy_default=default,
            auto_approval_criteria_default=auto_approval_criteria,
            auto_mode_evaluator=auto_mode_evaluator,
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
    assert steer.disposition is MessageDisposition.JOINED

    # Nothing was queued — a join does not take a queue slot. (We deliberately do NOT assert
    # turn_participants == 2 here: `steer` returns immediately, so the count may already be
    # back to 1. That IS the point of ACCEPT — it ran now rather than waiting. The blocking
    # `outlive` handler is where the count is observable; see the sibling test below.)
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.turn_active is True
    assert status.pending_turns == []

    # A JOIN CONSUMES NO TURN SLOT: however many messages join, the agent is still on the
    # same turn with nothing queued behind it.
    again = await _send(handle, "steer", {"text": "and again"})
    assert again.turn_id == work.turn_id
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.current_turn == work.turn_number
    assert status.pending_turns == []

    await handle.signal(MidTurnProbeAgent.release, "work")
    events = await _collect_until_turn_end(client, handle.id)

    ours = [e for e in events if e.turn_id == work.turn_id]
    types = [e.event.type for e in ours]
    assert types.count(AgentEventType.TURN_STARTED) == 1
    assert types.count(AgentEventType.TURN_END) == 1
    assert types[-1] == AgentEventType.TURN_END
    # Both participants replied inside the one bracket.
    replies = [
        e.event.output
        for e in ours
        if e.event.type == AgentEventType.MESSAGE_HANDLER_END
    ]
    assert len(replies) == 3  # the opener plus both joins
    assert {r["text"].split(":")[0] for r in replies} == {"worked", "steer"}


async def test_shared_turn_pairs_each_reply_with_its_own_message(client_and_queue):
    """The reason ``message_id`` exists: two participants, one turn, unambiguous replies.

    Under one ``turn_id`` there are now two ``message_handler_end`` events and no ordering
    guarantee about which comes first — the opener here deliberately finishes LAST. Anything
    pairing a reply with the message that produced it must key on ``message_id``.
    """
    client, task_queue = client_and_queue
    handle, work = await _hold_a_turn_open(client, task_queue)

    joined = await _send(handle, "outlive", {"text": "after you"})
    assert joined.turn_id == work.turn_id
    assert joined.message_id != work.message_id

    # Let the JOINED participant finish first, so the opener's reply is not simply last.
    await handle.signal(MidTurnProbeAgent.release, "outlive")
    await handle.signal(MidTurnProbeAgent.release, "work")
    events = await _collect_until_turn_end(client, handle.id)

    ours = [e for e in events if e.turn_id == work.turn_id]
    replies = {
        e.message_id: e.event.output["text"]
        for e in ours
        if e.event.type == AgentEventType.MESSAGE_HANDLER_END
        # The `outlive` handler also publishes a bare reply of its own to prove the turn is
        # still open; skip that one by keying on the handler's real return shape.
        and "text" in e.event.output
    }
    assert replies[work.message_id].startswith("worked:")
    assert replies[joined.message_id].startswith("outlived:")

    # One bracket for the turn, and it belongs to neither message.
    brackets = [
        e
        for e in ours
        if e.event.type in (AgentEventType.TURN_STARTED, AgentEventType.TURN_END)
    ]
    assert len(brackets) == 2
    assert all(e.message_id is None for e in brackets)

    # Every other event of the turn is attributed to one of the two participants.
    assert {
        e.message_id
        for e in ours
        if e.event.type not in (AgentEventType.TURN_STARTED, AgentEventType.TURN_END)
    } == {work.message_id, joined.message_id}


async def test_message_accepted_reports_what_the_message_did_to_the_turn(client_and_queue):
    """``message_accepted`` is the only event carrying the message, and it says its fate.

    All three dispositions, on one agent: idle open, queued behind the open turn, and joined
    into it. The event's ``turn_id`` names the turn in question either way — already running
    for a join, still to start for the other two.
    """
    client, task_queue = client_and_queue
    handle, work = await _hold_a_turn_open(client, task_queue)
    queued = await _send(handle, "work", {"text": "next"})
    joined = await _send(handle, "steer", {"text": "ride along"})

    await handle.signal(MidTurnProbeAgent.release, "work")
    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe_all(client, handle.id):
            events.append(item.data)
            # Two turns run here (the open one, then the queued one), so stop on the second.
            if (
                item.data.event.type == AgentEventType.TURN_END
                and item.data.turn_number == queued.turn_number
            ):
                break

    accepted = {
        e.message_id: e.event
        for e in events
        if e.event.type == AgentEventType.MESSAGE_ACCEPTED
    }
    assert accepted[work.message_id].disposition is MessageDisposition.OPENED
    assert accepted[queued.message_id].disposition is MessageDisposition.QUEUED
    assert accepted[joined.message_id].disposition is MessageDisposition.JOINED

    # The payload rides along structurally — no rendering, no re-parsing.
    assert accepted[joined.message_id].handler == "steer"
    assert accepted[joined.message_id].payload == {"text": "ride along"}

    # Admission precedes everything the message causes, including the turn it opens.
    order = [(e.message_id, e.event.type) for e in events]
    assert order.index((work.message_id, AgentEventType.MESSAGE_ACCEPTED)) < order.index(
        (None, AgentEventType.TURN_STARTED)
    )
    # ...and a join's admission lands INSIDE the turn it joined, after that turn started.
    assert order.index((None, AgentEventType.TURN_STARTED)) < order.index(
        (joined.message_id, AgentEventType.MESSAGE_ACCEPTED)
    )


async def test_message_handler_start_brackets_only_the_handler(client_and_queue):
    """Admission and execution are separate events, and the gap between them is queue latency.

    A queued message is accepted immediately but does not start until the turn ahead of it
    finishes — which is exactly the interval a debugging UI wants, and which a single folded
    event would leave to be reverse-engineered from turn timestamps.
    """
    client, task_queue = client_and_queue
    handle, _work = await _hold_a_turn_open(client, task_queue)
    queued = await _send(handle, "work", {"text": "next"})

    # Accepted already — while the agent is still busy with the turn ahead of it.
    events = await _collect_events(client, handle.id)
    assert any(
        e.event.type == AgentEventType.MESSAGE_ACCEPTED
        and e.message_id == queued.message_id
        for e in events
    )
    assert not any(
        e.event.type == AgentEventType.MESSAGE_HANDLER_START
        and e.message_id == queued.message_id
        for e in events
    )

    await handle.signal(MidTurnProbeAgent.release, "work")
    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe_all(client, handle.id):
            events.append(item.data)
            if (
                item.data.event.type == AgentEventType.MESSAGE_HANDLER_START
                and item.data.message_id == queued.message_id
            ):
                break
    ours = [e for e in events if e.message_id == queued.message_id]
    assert [e.event.type for e in ours] == [
        AgentEventType.MESSAGE_ACCEPTED,
        AgentEventType.MESSAGE_HANDLER_START,
    ]


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
    assert idle.disposition is MessageDisposition.OPENED

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


async def test_send_message_fast_fails_on_a_join_instead_of_hanging(client_and_queue):
    """``send_message`` is one message → one turn → one stream, and says so immediately.

    A joining message has its turn's ``turn_started`` BEHIND its ``accepted_offset``, so the
    merge's skip preamble never matches and the caller would sit in silence until the turn
    timeout (300s) for a single ``AgentTurnTimeout``. The reply's ``disposition`` makes that
    knowable at submit time, so it raises instead — and the message is still running, which is
    what the carried reply is for.
    """
    client, task_queue = client_and_queue
    handle, work = await _hold_a_turn_open(client, task_queue)
    agent_client = AgentClient(client, handle.id)

    with pytest.raises(JoinedTurnError) as excinfo:
        await agent_client.send_message(
            "steer",
            {"text": "ride along"},
            on_item=lambda item, _offset: item,
        )

    # Not a rejection: it joined, it is running, and the caller can find it on a stream by
    # the message_id the exception carries.
    reply = excinfo.value.reply
    assert reply.disposition is MessageDisposition.JOINED
    assert reply.turn_id == work.turn_id
    assert reply.message_id

    await handle.signal(MidTurnProbeAgent.release, "work")
    events = await _collect_until_turn_end(client, handle.id)
    joined_replies = [
        e
        for e in events
        if e.message_id == reply.message_id
        and e.event.type == AgentEventType.MESSAGE_HANDLER_END
    ]
    assert len(joined_replies) == 1


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


async def test_resend_under_the_same_update_id_is_one_message(client_and_queue):
    """Two submits under one Temporal update id are one admission: the same reply comes
    back and the handler runs once. This is what the subagent-turn activity relies on to
    make a retried send safe."""
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, TypedProbeAgent)

    first = await _send(handle, "greet", {"name": "Ada"}, update_id="send-1")
    retried = await _send(handle, "greet", {"name": "Ada"}, update_id="send-1")
    assert retried == first

    # A different id IS a different message, even with an identical payload.
    second = await _send(handle, "greet", {"name": "Ada"}, update_id="send-2")
    assert second.message_id != first.message_id

    assert await _wait_for_seen(handle, 2) == ["greet:Ada", "greet:Ada"]
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    assert status.current_turn == 2 and status.pending_turns == []
