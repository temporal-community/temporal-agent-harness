# ABOUTME: End-to-end tests for the safe-by-default, policy-driven human-in-the-loop tool
# approvals, run against the Temporal time-skipping test server (the only faithful way —
# the in-workflow approval gate + activity-side publishing only exist when a real workflow
# + activity execute). Demonstrates docs/internal/human-in-the-loop-tool-approvals.md:
#   * a gated tool runs only AFTER approval (approval_requested -> resolved(approved) ->
#     tool_start -> tool_end); a denied one never executes (ToolApprovalDenied);
#   * the agent's ToolApprovalPolicy decides gating, NOT the tool: an inherently_safe tool
#     runs without a gate under allow_inherently_safe, yet is still gated under
#     always_require_human_approval; an allow-listed / dangerously-skipped tool runs ungated;
#   * a caller's AgentConfig.approval_policy overrides the agent's built-in default;
#   * "approve, and don't ask again" (remember=True) allow-lists the tool, cascading to a
#     concurrently-pending call of the same tool, and is reflected on the status query;
#   * an auto mode evaluator approves a call the serializable policy did not, and the
#     harness brackets EVERY evaluator with auto_approval_evaluation_started -> _ended /
#     _superseded / _error, so an automatic decision is auditable whatever it decided —
#     including an ESCALATE, which resolves nothing and would otherwise leave no trace;
#   * an evaluator can DENY outright or escalate to the human gate, must be async, fails
#     SAFE when it raises or returns nonsense, and is CANCELLED (and published as
#     superseded) when a human decides while it is still thinking;
#   * pending approvals are discoverable via agent_status; the update is idempotent;
#   * an unresolved approval auto-denies on close (no hang); inline tool_defn gates too.
#
# Run with: uv run pytest harness/test_tool_approvals.py -v

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent import (
    AutoApprovalCriteria,
    AutoApprovalCriteriaSet,
    AutoApprovalVerdict,
    AutoApprovalDecision,
    AutoApprovalContext,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_client import AgentClient, ToolApprovalError
from temporal_agent_harness.harness.agent_protocol import (
    APPROVAL_SHORT_ID_LENGTH,
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    TextMessage,
    TextReply,
    AgentMessageReply,
)


# ---------------------------------------------------------------------------
# Tools under test — a non-safe activity tool, an inherently-safe activity tool,
# and a non-safe inline workflow tool. None decide their own gating; the agent's
# ToolApprovalPolicy does. `inherently_safe` is only a static hint.
# ---------------------------------------------------------------------------


@agent.activity_tool_defn()
async def gated_activity_tool(text: str) -> str:
    """A non-safe activity tool: gated unless the policy allows it."""
    return f"act:{text}"


@agent.activity_tool_defn(inherently_safe=True)
async def safe_activity_tool(text: str) -> str:
    """An inherently-safe activity tool (e.g. read-only)."""
    return f"safe:{text}"


@agent.tool_defn()
async def gated_workflow_tool(text: str) -> str:
    """A non-safe inline tool: runs in-process, gated unless the policy allows it."""
    return f"wf:{text}"


async def _approve_gated_activity_tool(ctx: AutoApprovalContext) -> AutoApprovalDecision:
    """A minimal auto mode evaluator: approve only ``gated_activity_tool``."""
    return AutoApprovalDecision(
        AutoApprovalVerdict.APPROVE
        if ctx.tool_name == "gated_activity_tool"
        else AutoApprovalVerdict.ESCALATE,
        reason="auto-approved by the evaluator",
    )


async def _verdict_evaluator(ctx: AutoApprovalContext) -> AutoApprovalDecision:
    """An evaluator standing in for an AI one.

    It awaits before answering (as a real one must — asking a model is an activity call),
    and exercises all three verdicts off the call's own arguments: ``deny`` refuses the
    call outright, ``escalate`` abstains to the human gate, anything else is approved.
    ``tool_description`` is asserted through, since the tool's docstring is the main thing
    an AI evaluator has to reason about."""
    await asyncio.sleep(0)
    assert ctx.tool_description == "A non-safe activity tool: gated unless the policy allows it."
    text = ctx.tool_input.get("text")
    if text == "deny":
        return AutoApprovalDecision(AutoApprovalVerdict.DENY, reason="policy forbids 'deny'")
    if text == "escalate":
        return AutoApprovalDecision(AutoApprovalVerdict.ESCALATE, reason="not sure")
    return AutoApprovalDecision(AutoApprovalVerdict.APPROVE, reason="looks fine")


async def _exploding_evaluator(ctx: AutoApprovalContext) -> AutoApprovalDecision:
    """A broken evaluator. The gate must escalate to a human, never fail open."""
    raise RuntimeError("approver is broken")


async def _nonsense_evaluator(ctx: AutoApprovalContext):
    """Returns something that is not an AutoApprovalDecision — a developer bug, which must
    fail the same safe way a raised exception does rather than approving or crashing."""
    return "yes please"


# ---------------------------------------------------------------------------
# Probe workflows — each turn's text selects a scenario. Tool call ids are fixed
# so a test can address approvals deterministically. The default policy gates
# everything (always_require_human_approval); tests relax it via AgentConfig.
# ---------------------------------------------------------------------------


class _BaseProbe:
    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Run the tool scenario selected by the message text."""
        return TextReply(text=await self._handle(message.text))

    async def _handle(self, text: str) -> str:
        if text == "concurrent":
            # Two gated calls of the SAME tool at once, distinct ids. They wait
            # independently; whichever is approved first executes first.
            results = await asyncio.gather(
                self._runner.run_tool("act-A", gated_activity_tool, "A"),
                self._runner.run_tool("act-B", gated_activity_tool, "B"),
            )
            self._last_reply = "|".join(results)
            return self._last_reply

        if text == "safe":
            call_id, tool, arg = "s1", safe_activity_tool, "S"
        elif text.startswith("arg:"):
            # One gated call whose ARGUMENT the message chooses, so a custom fallback can
            # reach a different verdict per call the way a real one does.
            call_id, tool, arg = "g1", gated_activity_tool, text.removeprefix("arg:")
        elif text == "workflow-tool":
            call_id, tool, arg = "wf-1", gated_workflow_tool, "Z"
        else:
            call_id, tool, arg = "g1", gated_activity_tool, "S"

        # Mirror the real agent loop: a denied (or close-auto-denied) call surfaces as a
        # result rather than failing the turn.
        try:
            self._last_reply = await self._runner.run_tool(call_id, tool, arg)
        except agent.ToolApprovalDenied as e:
            self._last_reply = f"denied:{e.reason}"
        return self._last_reply


@workflow.defn
@agent.defn
class ApprovalProbeAgent(_BaseProbe):
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Safe-by-default baseline: gate everything. Tests relax it per session via
            # AgentConfig.approval_policy.
            approval_policy_default=ToolApprovalPolicy.always_require_human_approval(),
        )
        # Last reply text, exposed via a query so a test can assert the outcome even after
        # the workflow has COMPLETED (the live event stream is gone by then — relevant for
        # the close-while-pending case, which ends the workflow).
        self._last_reply: str | None = None

    @workflow.query
    def last_reply(self) -> str | None:
        return self._last_reply

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)


# Auto mode only reaches an evaluator for a call a criteria set GOVERNS — a custom evaluator
# is no exception, because criteria are the schema every implementation reads rather than a
# convenience for the Jev one. A catch-all is the least a probe can configure and still be
# asked. The evaluators below ignore the rules' content and decide on tool name alone, which
# is legal: what the harness guarantees is that an evaluator is never handed an UNGOVERNED
# call, not that it must use what it is handed.
_EVALUATED = AutoApprovalCriteria(
    sets={
        "evaluated": AutoApprovalCriteriaSet(
            effect="Whatever the probe's tool does.",
            escalate_when=("the evaluator under test says so",),
        )
    },
    default="evaluated",
)


@workflow.defn
@agent.defn
class EvaluatorProbeAgent(_BaseProbe):
    """Gates everything by default, but wires a custom fallback that auto-approves
    ``gated_activity_tool`` — the FINAL approval layer."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.auto_mode(),
            auto_approval_criteria_default=_EVALUATED,
            auto_mode_evaluator=_approve_gated_activity_tool,
        )
        self._last_reply: str | None = None

    @workflow.query
    def last_reply(self) -> str | None:
        return self._last_reply

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)


@workflow.defn
@agent.defn
class VerdictEvaluatorProbeAgent(_BaseProbe):
    """Gates everything by default, with an ASYNC three-valued fallback wired — the shape
    a Jev auto-approver takes."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.auto_mode(),
            auto_approval_criteria_default=_EVALUATED,
            auto_mode_evaluator=_verdict_evaluator,
        )
        self._last_reply: str | None = None

    @workflow.query
    def last_reply(self) -> str | None:
        return self._last_reply

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)


@workflow.defn
@agent.defn
class SlowEvaluatorProbeAgent(_BaseProbe):
    """A fallback that does not answer until the test says so, then DENIES.

    Stands in for the real timing of an AI approver — which spends an activity round-trip
    thinking while the gate is already open — but under the test's control, so the "a human
    decided first" ordering is asserted rather than raced for."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._released = False
        self._evaluator_cancelled = False
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.auto_mode(),
            auto_approval_criteria_default=_EVALUATED,
            auto_mode_evaluator=self._deny_once_released,
        )
        self._last_reply: str | None = None

    async def _deny_once_released(self, ctx: AutoApprovalContext) -> AutoApprovalDecision:
        try:
            await workflow.wait_condition(lambda: self._released)
        except asyncio.CancelledError:
            # Recorded so a test can prove the CORO ITSELF was cancelled, not merely that
            # its verdict was ignored — the difference between stopping the work and
            # paying for it anyway.
            self._evaluator_cancelled = True
            raise
        return AutoApprovalDecision(AutoApprovalVerdict.DENY, reason="approver says no")

    @workflow.query
    def evaluator_cancelled(self) -> bool:
        return self._evaluator_cancelled

    @workflow.signal
    def release_approver(self) -> None:
        self._released = True

    @workflow.query
    def last_reply(self) -> str | None:
        return self._last_reply

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)


@workflow.defn
@agent.defn
class NonsenseEvaluatorProbeAgent(_BaseProbe):
    """An evaluator that returns the wrong type — a developer bug, not an exception."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.auto_mode(),
            auto_approval_criteria_default=_EVALUATED,
            auto_mode_evaluator=_nonsense_evaluator,
        )
        self._last_reply: str | None = None

    @workflow.query
    def last_reply(self) -> str | None:
        return self._last_reply

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)


@workflow.defn
@agent.defn
class BrokenEvaluatorProbeAgent(_BaseProbe):
    """Gates everything, with a fallback that raises — the guardrail must fail SAFE."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.auto_mode(),
            auto_approval_criteria_default=_EVALUATED,
            auto_mode_evaluator=_exploding_evaluator,
        )
        self._last_reply: str | None = None

    @workflow.query
    def last_reply(self) -> str | None:
        return self._last_reply

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)


# ---------------------------------------------------------------------------
# Fixture + helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def env_and_client():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"approval-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[
            ApprovalProbeAgent,
            EvaluatorProbeAgent,
            VerdictEvaluatorProbeAgent,
            SlowEvaluatorProbeAgent,
            BrokenEvaluatorProbeAgent,
            NonsenseEvaluatorProbeAgent,
        ],
        activities=[
            agent.tool_activity(gated_activity_tool),
            agent.tool_activity(safe_activity_tool),
        ],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _start(
    client: Client,
    task_queue: str,
    *,
    config: AgentConfig | None = None,
    workflow_cls: type = ApprovalProbeAgent,
):
    handle = await client.start_workflow(
        workflow_cls.run,
        config if config is not None else AgentConfig(),
        id=f"{workflow_cls.__name__}-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    return handle


async def _send(handle, text: str) -> AgentMessageReply:
    return await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="act", payload={"text": text}),
        result_type=AgentMessageReply,
    )


def _subscribe(client: Client, workflow_id: str):
    stream = WorkflowStreamClient.create(client, workflow_id)
    return stream.subscribe(
        topics=[TURN_EVENTS_TOPIC],
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    )


async def _drain_to_turn_end(client: Client, workflow_id: str) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, workflow_id):
            events.append(item.data)
            if item.data.event.type == AgentEventType.TURN_END:
                break
    return events


def _types_for(events: list[AgentEvent], tool_id: str) -> list[str]:
    return [
        e.event.type for e in events if getattr(e.event, "tool_id", None) == tool_id
    ]


def _event(events: list[AgentEvent], event_type: str):
    """The one event of ``event_type``, asserting there is exactly one."""
    matches = [e.event for e in events if e.event.type == event_type]
    assert len(matches) == 1, f"expected exactly one {event_type}, got {len(matches)}"
    return matches[0]


def _assert_bracket_pairs(events: list[AgentEvent], *, evaluator: str) -> None:
    """The evaluation bracket is well formed: one start, one terminal, same evaluation_id.

    ``evaluation_id`` — not ``tool_id`` — is what pairs them, so this is the guard that the
    pairing key is actually carried and actually matches.
    """
    started = _event(events, AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED)
    terminals = [
        e.event
        for e in events
        if e.event.type
        in (
            AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED,
            AgentEventType.AUTO_APPROVAL_EVALUATION_SUPERSEDED,
            AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR,
        )
    ]
    assert len(terminals) == 1
    assert started.evaluator == evaluator == terminals[0].evaluator
    assert started.evaluation_id == terminals[0].evaluation_id
    assert started.evaluation_id


def _reply_text(events: list[AgentEvent]) -> str:
    # The probes reply with TextReply(text=...), so the reply's output dict carries `text`.
    reply = next(e.event for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END)
    return reply.output["text"]


# ---------------------------------------------------------------------------
# Core gate lifecycle
# ---------------------------------------------------------------------------


async def test_approved_tool_executes_after_approval(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "single")
    agent_client = AgentClient(client, handle.id)

    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == "g1"
            ):
                # g1 input is the model-facing input (no injected params here).
                assert ev.event.tool_input == {"text": "S"}
                await agent_client.approve_tool("g1", approved=True)
            if ev.event.type == AgentEventType.TURN_END:
                break

    # The full gated lifecycle, in order: approval requested -> resolved(approved)
    # -> start -> end. (No tool_requested here: that comes from the streaming model
    # activity, which this probe has no model in the loop for.)
    assert _types_for(events, "g1") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    resolved = next(
        e.event for e in events if e.event.type == AgentEventType.TOOL_APPROVAL_RESOLVED
    )
    assert resolved.approved is True
    assert _reply_text(events) == "act:S"


async def test_every_event_of_a_dispatch_carries_its_message_id(env_and_client):
    """Envelope-side attribution, end to end — the point of putting ``message_id`` there.

    A gated ACTIVITY tool exercises every publisher at once: the workflow (approval requested,
    tool lifecycle), the update handler resolving the gate from OUTSIDE the participant, and
    the activity, which publishes across a process boundary where a ContextVar cannot reach.
    All three must stamp the same id, and only the two turn brackets may be unattributed.
    """
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    reply = await _send(handle, "single")
    agent_client = AgentClient(client, handle.id)

    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == "g1"
            ):
                await agent_client.approve_tool("g1", approved=True)
            if ev.event.type == AgentEventType.TURN_END:
                break

    assert reply.message_id
    brackets = {AgentEventType.TURN_STARTED, AgentEventType.TURN_END}
    for ev in events:
        expected = None if ev.event.type in brackets else reply.message_id
        assert ev.message_id == expected, ev.event.type

    # Not a vacuous pass: the interesting publishers really are in there.
    seen = {e.event.type for e in events}
    assert {
        AgentEventType.MESSAGE_ACCEPTED,
        AgentEventType.MESSAGE_HANDLER_START,
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        # Published by the ``tool_approval`` UPDATE handler, which is in no participant task —
        # it reads the id off the approval entry, so a policy cascade resolving several calls
        # attributes each to the message that made it.
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        # Published from inside the activity, via TurnStreamContext.
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
        AgentEventType.MESSAGE_HANDLER_END,
    } <= seen


async def test_denied_tool_does_not_execute(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "single")
    agent_client = AgentClient(client, handle.id)

    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == "g1"
            ):
                await agent_client.approve_tool(
                    "g1", approved=False, reason="not allowed"
                )
            if ev.event.type == AgentEventType.TURN_END:
                break

    # Denied: resolved(approved=False) and NO start/end — the activity never ran.
    assert _types_for(events, "g1") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
    ]
    resolved = next(
        e.event for e in events if e.event.type == AgentEventType.TOOL_APPROVAL_RESOLVED
    )
    assert resolved.approved is False and resolved.reason == "not allowed"
    assert _reply_text(events) == "denied:not allowed"


# ---------------------------------------------------------------------------
# Policy layers — the policy (not the tool) decides gating
# ---------------------------------------------------------------------------


async def test_inherently_safe_tool_auto_approves_under_allow_safe(env_and_client):
    """Under allow_inherently_safe, a safe tool runs with NO gate — no approval events,
    straight to start/end."""
    client, task_queue = env_and_client
    handle = await _start(
        client,
        task_queue,
        config=AgentConfig(approval_policy=ToolApprovalPolicy.allow_inherently_safe()),
    )
    await _send(handle, "safe")
    events = await _drain_to_turn_end(client, handle.id)

    assert _types_for(events, "s1") == [
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    assert _reply_text(events) == "safe:S"


async def test_always_require_gates_even_inherently_safe(env_and_client):
    """The safe-by-default baseline gates even an inherently-safe tool (step-through)."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)  # default = always_require_human_approval
    await _send(handle, "safe")
    agent_client = AgentClient(client, handle.id)

    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == "s1"
            ):
                await agent_client.approve_tool("s1", approved=True)
            if ev.event.type == AgentEventType.TURN_END:
                break

    assert _types_for(events, "s1") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    assert _reply_text(events) == "safe:S"


async def test_allow_listed_tool_auto_approves(env_and_client):
    """A tool named in auto_approve_tools runs ungated."""
    client, task_queue = env_and_client
    handle = await _start(
        client,
        task_queue,
        config=AgentConfig(
            approval_policy=ToolApprovalPolicy.allow_tools(["gated_activity_tool"])
        ),
    )
    await _send(handle, "single")
    events = await _drain_to_turn_end(client, handle.id)

    assert _types_for(events, "g1") == [
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    assert _reply_text(events) == "act:S"


async def test_dangerously_skip_all_auto_approves(env_and_client):
    """dangerously_skip_all runs every tool ungated."""
    client, task_queue = env_and_client
    handle = await _start(
        client,
        task_queue,
        config=AgentConfig(approval_policy=ToolApprovalPolicy.dangerously_skip_all()),
    )
    await _send(handle, "single")
    events = await _drain_to_turn_end(client, handle.id)

    assert _types_for(events, "g1") == [
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    assert _reply_text(events) == "act:S"


async def test_config_policy_overrides_agent_default(env_and_client):
    """The agent's default gates everything; a caller's config policy wins, so the same
    tool runs ungated. Also surfaced on the status query."""
    client, task_queue = env_and_client
    handle = await _start(
        client,
        task_queue,
        config=AgentConfig(approval_policy=ToolApprovalPolicy.dangerously_skip_all()),
    )
    agent_client = AgentClient(client, handle.id)
    status = await agent_client.get_status()
    assert status.approval_policy == ToolApprovalPolicy.dangerously_skip_all()
    assert status.has_auto_approval_evaluator is False

    await _send(handle, "single")
    events = await _drain_to_turn_end(client, handle.id)
    assert AgentEventType.TOOL_APPROVAL_REQUESTED not in _types_for(events, "g1")
    assert _reply_text(events) == "act:S"


async def test_custom_fallback_approves_what_policy_did_not(env_and_client):
    """The custom fallback auto-approves gated_activity_tool though the policy
    (always_require) did not — so it runs without a human ever being asked.

    Its verdict is PUBLISHED, unlike a policy allow-list's: an approval nobody consented
    to has to be answerable after the fact, so the call is registered and announced first
    and the fallback's decision lands as a normal resolution with a reason. (A call the
    POLICY approves stays silent — see test_dangerously_skip_all_runs_ungated.)"""
    client, task_queue = env_and_client
    handle = await _start(
        client, task_queue, workflow_cls=EvaluatorProbeAgent
    )
    agent_client = AgentClient(client, handle.id)
    assert (await agent_client.get_status()).has_auto_approval_evaluator is True

    await _send(handle, "single")
    events = await _drain_to_turn_end(client, handle.id)
    assert _types_for(events, "g1") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    # Even a plain bool predicate gets the bracket — the harness publishes it, so a
    # developer gets the audit trail by wiring a fallback at all, not by instrumenting one.
    _assert_bracket_pairs(events, evaluator="_approve_gated_activity_tool")
    ended = _event(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.verdict is AutoApprovalVerdict.APPROVE
    assert ended.details == {}  # this evaluator records no structured reasoning
    resolved = _event(events, AgentEventType.TOOL_APPROVAL_RESOLVED)
    assert resolved.approved is True
    assert resolved.reason == "auto-approved by the evaluator"
    assert _reply_text(events) == "act:S"


# ---------------------------------------------------------------------------
# Async, three-valued fallbacks — the shape an AI auto-approver takes
# ---------------------------------------------------------------------------


async def test_async_fallback_approves_with_its_reason(env_and_client):
    """An ASYNC fallback returning APPROVE dispatches the call, and its reason is the
    audit trail on the resolution."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue, workflow_cls=VerdictEvaluatorProbeAgent)

    await _send(handle, "arg:ok")
    events = await _drain_to_turn_end(client, handle.id)
    assert _types_for(events, "g1") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    _assert_bracket_pairs(events, evaluator="_verdict_evaluator")
    ended = _event(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert (ended.verdict, ended.reason) == (AutoApprovalVerdict.APPROVE, "looks fine")
    resolved = _event(events, AgentEventType.TOOL_APPROVAL_RESOLVED)
    assert (resolved.approved, resolved.reason) == (True, "looks fine")
    assert _reply_text(events) == "act:ok"

    # The bracket is what makes the evaluator's own latency measurable: its duration is the
    # gap between the two envelope timestamps, not something inferred from a neighbouring
    # event of a different kind.
    start_ts = next(
        e.timestamp
        for e in events
        if e.event.type == AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED
    )
    end_ts = next(
        e.timestamp
        for e in events
        if e.event.type == AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED
    )
    assert end_ts >= start_ts


async def test_async_fallback_denies_outright(env_and_client):
    """DENY is a real verdict, not just "don't approve": the tool NEVER executes, the
    resolution is published as a denial, and the reason reaches the model as the error —
    so the turn continues and the agent can try something allowed instead."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue, workflow_cls=VerdictEvaluatorProbeAgent)

    await _send(handle, "arg:deny")
    events = await _drain_to_turn_end(client, handle.id)
    # Ends at the resolution — no tool_start, so the activity was never dispatched.
    assert _types_for(events, "g1") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
    ]
    ended = _event(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.verdict is AutoApprovalVerdict.DENY
    resolved = _event(events, AgentEventType.TOOL_APPROVAL_RESOLVED)
    assert (resolved.approved, resolved.reason) == (False, "policy forbids 'deny'")
    assert _reply_text(events) == "denied:policy forbids 'deny'"


async def test_async_fallback_escalates_to_the_human_gate(env_and_client):
    """ESCALATE abstains: the call stays PENDING and waits for a person, exactly as if no
    fallback were wired — and a human approval then releases it."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue, workflow_cls=VerdictEvaluatorProbeAgent)
    agent_client = AgentClient(client, handle.id)
    await _send(handle, "arg:escalate")

    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == "g1"
            ):
                # Still pending after the fallback abstained — the gate is the human's.
                pending = (await agent_client.get_status()).pending_approvals
                assert [p.tool_id for p in pending] == ["g1"]
                await agent_client.approve_tool("g1", approved=True, reason="I'll allow it")
            if ev.event.type == AgentEventType.TURN_END:
                break

    assert _types_for(events, "g1") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    resolved = _event(events, AgentEventType.TOOL_APPROVAL_RESOLVED)
    # The HUMAN's reason, not the fallback's "not sure" — the fallback never resolved it.
    assert (resolved.approved, resolved.reason) == (True, "I'll allow it")

    # THE POINT OF THE BRACKET. An escalate resolves nothing, so without its own event the
    # only record of an evaluator that ran, cost a round-trip and declined to decide would
    # be the human resolution above — attributed entirely to the human. Here the abstention
    # and its reason are on the stream in their own right.
    _assert_bracket_pairs(events, evaluator="_verdict_evaluator")
    ended = _event(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert (ended.verdict, ended.reason) == (AutoApprovalVerdict.ESCALATE, "not sure")
    assert _reply_text(events) == "act:escalate"


async def test_a_human_who_decides_first_cancels_the_evaluator(env_and_client):
    """Whoever resolves the entry first wins — and the loser is STOPPED, not just ignored.

    The gate is published before the evaluator runs precisely so a person can answer while
    it thinks. When they do, an evaluator left running would keep burning a model call (and
    an activity's worker slot) on a question nobody is waiting for. The harness cancels its
    coroutine and closes the bracket on ``superseded``, never on ``ended`` — a verdict
    published as ended is one that was acted on.

    The probe's evaluator records its own CancelledError, so this asserts the coroutine
    really was cancelled rather than merely overruled."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue, workflow_cls=SlowEvaluatorProbeAgent)
    agent_client = AgentClient(client, handle.id)
    await _send(handle, "single")

    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                ev.event.type == AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED
                and ev.event.tool_id == "g1"
            ):
                # The evaluator is definitely mid-flight: it is parked on a condition only
                # release_approver can satisfy, and this test never sends it.
                await agent_client.approve_tool(
                    "g1", approved=True, reason="human got there first"
                )
            if ev.event.type == AgentEventType.TURN_END:
                break

    assert _types_for(events, "g1") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_SUPERSEDED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    _assert_bracket_pairs(
        events, evaluator="SlowEvaluatorProbeAgent._deny_once_released"
    )
    superseded = _event(events, AgentEventType.AUTO_APPROVAL_EVALUATION_SUPERSEDED)
    # Cut off before it could answer, which is the normal shape of a cancellation.
    assert superseded.verdict is None
    # The coroutine itself saw the cancellation — the work stopped, it was not merely
    # ignored while it ran on.
    assert await handle.query(SlowEvaluatorProbeAgent.evaluator_cancelled) is True

    resolved = _event(events, AgentEventType.TOOL_APPROVAL_RESOLVED)
    assert (resolved.approved, resolved.reason) == (True, "human got there first")
    assert _reply_text(events) == "act:S"


async def test_an_evaluator_returning_the_wrong_type_also_fails_safe(env_and_client):
    """A wrong return type is the same class of problem as a raised exception — the
    evaluator did not produce a decision — so it takes the same path: an ERROR terminal on
    the bracket, an escalate to the human gate, and never an approval. It must not
    propagate out of the gate either, which would fail the tool call with something that
    is not an approval decision at all."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue, workflow_cls=NonsenseEvaluatorProbeAgent)
    agent_client = AgentClient(client, handle.id)
    await _send(handle, "single")

    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == "g1"
            ):
                await agent_client.approve_tool("g1", approved=False, reason="nope")
            if ev.event.type == AgentEventType.TURN_END:
                break

    assert _types_for(events, "g1") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
    ]
    failed = _event(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    assert "must return an AutoApprovalDecision" in failed.message
    assert _reply_text(events) == "denied:nope"


async def test_a_broken_evaluator_escalates_instead_of_failing_open(env_and_client):
    """An evaluator that RAISES must not approve, and must not wedge the turn either: the
    call falls through to the human gate, where a person denies it."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue, workflow_cls=BrokenEvaluatorProbeAgent)
    agent_client = AgentClient(client, handle.id)
    await _send(handle, "single")

    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == "g1"
            ):
                await agent_client.approve_tool("g1", approved=False, reason="no")
            if ev.event.type == AgentEventType.TURN_END:
                break

    # Never dispatched: the broken approver did not wave it through.
    assert _types_for(events, "g1") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
    ]
    # The bracket closes on the ERROR terminal, not on an "ended" carrying a fake verdict:
    # a broken approver is a failed evaluation, not an abstention, and "everything silently
    # went to humans for three hours" should be visible as exactly that.
    _assert_bracket_pairs(events, evaluator="_exploding_evaluator")
    failed = _event(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    assert "approver is broken" in failed.message
    assert _reply_text(events) == "denied:no"


# ---------------------------------------------------------------------------
# Runtime relaxation — "approve, and don't ask me about this tool again"
# ---------------------------------------------------------------------------


async def test_remember_allowlists_tool_and_cascades_to_pending(env_and_client):
    """Two concurrent gated calls of the same tool. Approving the FIRST with
    remember=True allow-lists the tool, which auto-resolves the SECOND with no explicit
    decision — and the live policy now lists the tool (so future calls skip the gate)."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "concurrent")
    agent_client = AgentClient(client, handle.id)

    requested: set[str] = set()
    remembered = False
    explicit_approvals = 0
    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED:
                requested.add(ev.event.tool_id)
                # Once BOTH are pending, approve act-A with remember; act-B should then
                # auto-resolve via the policy update — we never approve it explicitly.
                if {"act-A", "act-B"} <= requested and not remembered:
                    await agent_client.approve_tool(
                        "act-A", approved=True, remember=True
                    )
                    explicit_approvals += 1
                    remembered = True
            if ev.event.type == AgentEventType.TURN_END:
                break

    # Both executed...
    assert {
        e.event.tool_id for e in events if e.event.type == AgentEventType.TOOL_END
    } == {"act-A", "act-B"}
    # ...with only ONE explicit approval (act-B auto-resolved by the policy update).
    assert explicit_approvals == 1
    resolved = {
        e.event.tool_id: e.event
        for e in events
        if e.event.type == AgentEventType.TOOL_APPROVAL_RESOLVED
    }
    # act-A: the explicit "approve & remember" decision — its resolution carries remember.
    assert resolved["act-A"].approved is True
    assert resolved["act-A"].remember is True
    # act-B: swept up by the policy update — approved, but NOT itself a remember decision.
    assert resolved["act-B"].approved is True
    assert resolved["act-B"].remember is False
    assert resolved["act-B"].reason == "auto-approved by updated policy"
    assert _reply_text(events) == "act:A|act:B"

    # The live policy now allow-lists the tool, so a future call would skip the gate.
    policy = (await agent_client.get_status()).approval_policy
    assert "gated_activity_tool" in policy.auto_approve_tools


async def test_remember_resolution_is_causally_ordered_before_cascade(env_and_client):
    """Causal ordering: remember-approving the SECOND-requested call (act-B) must publish
    ITS resolution before the first-requested call (act-A) that its policy update then
    cascade-approves — even though act-A registered its gate first. The resolution event
    order follows the order decisions are made, not gate registration/wake order."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "concurrent")
    agent_client = AgentClient(client, handle.id)

    requested: set[str] = set()
    decided = False
    resolved_order: list[str] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            if ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED:
                requested.add(ev.event.tool_id)
                # Approve the SECOND-registered call (act-B) — the harder ordering case.
                if {"act-A", "act-B"} <= requested and not decided:
                    await agent_client.approve_tool("act-B", approved=True, remember=True)
                    decided = True
            elif ev.event.type == AgentEventType.TOOL_APPROVAL_RESOLVED:
                resolved_order.append(ev.event.tool_id)
            if ev.event.type == AgentEventType.TURN_END:
                break

    # act-B (the explicit "approve & remember" decision) resolves first; act-A (swept up by
    # the resulting policy update) resolves after — matching the causal order.
    assert resolved_order == ["act-B", "act-A"], resolved_order


# ---------------------------------------------------------------------------
# Discovery, idempotency, concurrency, close, inline
# ---------------------------------------------------------------------------


async def test_pending_approval_visible_in_status(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "single")
    agent_client = AgentClient(client, handle.id)

    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            if (
                item.data.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and item.data.event.tool_id == "g1"
            ):
                break

    pending = await agent_client.get_pending_approvals()
    assert [p.tool_id for p in pending] == ["g1"]
    assert pending[0].tool_name == "gated_activity_tool"
    assert pending[0].tool_input == {"text": "S"}

    await agent_client.approve_tool("g1", approved=True)
    await _drain_to_turn_end(client, handle.id)
    assert await agent_client.get_pending_approvals() == []


async def test_pending_approvals_carry_a_short_id(env_and_client):
    """Every pending approval carries a ``short_id``: fixed-width lowercase hex, and distinct
    across concurrently-gated calls.

    This is the identity a chat-platform integration puts on an approve/deny button, where the
    provider-minted ``tool_id`` would not fit the platform's length-capped callback field.
    """
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "concurrent")
    agent_client = AgentClient(client, handle.id)

    requested: set[str] = set()
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            if item.data.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED:
                requested.add(item.data.event.tool_id)
                if {"act-A", "act-B"} <= requested:
                    break

    pending = await agent_client.get_pending_approvals()
    assert {p.tool_id for p in pending} == {"act-A", "act-B"}

    short_ids = [p.short_id for p in pending]
    assert all(
        re.fullmatch(rf"[0-9a-f]{{{APPROVAL_SHORT_ID_LENGTH}}}", sid) for sid in short_ids
    ), short_ids
    assert len(set(short_ids)) == len(short_ids), short_ids

    # It is an ALIAS, not a replacement: resolving still goes through the real tool_id, and the
    # short id is what a caller maps back from.
    by_short = {p.short_id: p.tool_id for p in pending}
    for sid in short_ids:
        await agent_client.approve_tool(by_short[sid], approved=True)
    await _drain_to_turn_end(client, handle.id)
    assert await agent_client.get_pending_approvals() == []


async def test_approval_is_idempotent(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "single")
    agent_client = AgentClient(client, handle.id)

    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            if (
                item.data.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and item.data.event.tool_id == "g1"
            ):
                break

    with pytest.raises(ToolApprovalError) as unknown:
        await agent_client.approve_tool("does-not-exist", approved=True)
    assert unknown.value.error_type == "UnknownToolApproval"

    await agent_client.approve_tool("g1", approved=True)
    with pytest.raises(ToolApprovalError) as dup:
        await agent_client.approve_tool("g1", approved=False)
    assert dup.value.error_type == "ToolApprovalAlreadyResolved"

    await _drain_to_turn_end(client, handle.id)


async def test_concurrent_first_approved_executes_first(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "concurrent")
    agent_client = AgentClient(client, handle.id)

    # Both act-A and act-B are requested. Approve B first; only approve A once B has
    # actually STARTED — proving B (approved first) executes before A (requested first
    # in dispatch order), independent of request order.
    requested: set[str] = set()
    approved_b = approved_a = False
    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            t = ev.event.type
            if t == AgentEventType.TOOL_APPROVAL_REQUESTED:
                requested.add(ev.event.tool_id)
                if {"act-A", "act-B"} <= requested and not approved_b:
                    await agent_client.approve_tool("act-B", approved=True)
                    approved_b = True
            elif (
                t == AgentEventType.TOOL_START
                and ev.event.tool_id == "act-B"
                and not approved_a
            ):
                await agent_client.approve_tool("act-A", approved=True)
                approved_a = True
            if t == AgentEventType.TURN_END:
                break

    starts = [
        e.event.tool_id for e in events if e.event.type == AgentEventType.TOOL_START
    ]
    assert starts == ["act-B", "act-A"], starts
    assert _reply_text(events) == "act:A|act:B"  # gather preserves arg order


async def test_close_while_pending_auto_denies(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "single")

    pre_close: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            pre_close.append(item.data)
            if (
                item.data.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and item.data.event.tool_id == "g1"
            ):
                await handle.signal("close")
                break
    assert not any(e.event.type == AgentEventType.TOOL_START for e in pre_close)

    async with asyncio.timeout(30):
        await handle.result()
    completed = client.get_workflow_handle(handle.id)
    last_reply = await completed.query("last_reply", result_type=str)
    assert last_reply == "denied:agent closed before approval"


async def test_inline_workflow_tool_gates(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "workflow-tool")
    agent_client = AgentClient(client, handle.id)

    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == "wf-1"
            ):
                await agent_client.approve_tool("wf-1", approved=True)
            if ev.event.type == AgentEventType.TURN_END:
                break

    assert _types_for(events, "wf-1") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    assert _reply_text(events) == "wf:Z"
