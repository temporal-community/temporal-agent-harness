# ABOUTME: End-to-end tests for the safe-by-default, policy-driven human-in-the-loop tool
# approvals, run against the Temporal time-skipping test server (the only faithful way —
# the in-workflow approval gate + activity-side publishing only exist when a real workflow
# + activity execute). Demonstrates docs/human-in-the-loop-tool-approvals.md:
#   * a gated tool runs only AFTER approval (approval_requested -> resolved(approved) ->
#     tool_start -> tool_end); a denied one never executes (ToolApprovalDenied);
#   * the agent's ToolApprovalPolicy decides gating, NOT the tool: an inherently_safe tool
#     runs without a gate under allow_inherently_safe, yet is still gated under
#     always_require_approvals; an allow-listed / dangerously-skipped tool runs ungated;
#   * a caller's AgentConfig.approval_policy overrides the agent's built-in default;
#   * "approve, and don't ask again" (remember=True) allow-lists the tool, cascading to a
#     concurrently-pending call of the same tool, and is reflected on the status query;
#   * a developer custom fallback approves a call the serializable policy did not;
#   * pending approvals are discoverable via agent_status; the update is idempotent;
#   * an unresolved approval auto-denies on close (no hang); inline tool_defn gates too.
#
# Run with: uv run pytest harness/test_tool_approvals.py -v

from __future__ import annotations

import asyncio
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
from temporal_agent_harness.harness.agent import ToolApprovalContext, ToolApprovalPolicy
from temporal_agent_harness.harness.agent_client import AgentClient, ToolApprovalError
from temporal_agent_harness.harness.agent_protocol import (
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


def _approve_gated_activity_tool(ctx: ToolApprovalContext) -> bool:
    """A developer custom fallback: auto-approve only ``gated_activity_tool``."""
    return ctx.tool_name == "gated_activity_tool"


# ---------------------------------------------------------------------------
# Probe workflows — each turn's text selects a scenario. Tool call ids are fixed
# so a test can address approvals deterministically. The default policy gates
# everything (always_require_approvals); tests relax it via AgentConfig.
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
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
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


@workflow.defn
@agent.defn
class CustomFallbackProbeAgent(_BaseProbe):
    """Gates everything by default, but wires a custom fallback that auto-approves
    ``gated_activity_tool`` — the FINAL approval layer."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
            custom_approval_fallback=_approve_gated_activity_tool,
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
        workflows=[ApprovalProbeAgent, CustomFallbackProbeAgent],
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


async def _send(handle, text: str, expected_turn: int) -> None:
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="act", payload={"text": text}, expected_turn=expected_turn),
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


def _reply_text(events: list[AgentEvent]) -> str:
    # The probes reply with TextReply(text=...), so the reply's output dict carries `text`.
    reply = next(e.event for e in events if e.event.type == AgentEventType.REPLY)
    return reply.output["text"]


# ---------------------------------------------------------------------------
# Core gate lifecycle
# ---------------------------------------------------------------------------


async def test_approved_tool_executes_after_approval(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "single", expected_turn=1)
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


async def test_denied_tool_does_not_execute(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "single", expected_turn=1)
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
    await _send(handle, "safe", expected_turn=1)
    events = await _drain_to_turn_end(client, handle.id)

    assert _types_for(events, "s1") == [
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    assert _reply_text(events) == "safe:S"


async def test_always_require_gates_even_inherently_safe(env_and_client):
    """The safe-by-default baseline gates even an inherently-safe tool (step-through)."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)  # default = always_require_approvals
    await _send(handle, "safe", expected_turn=1)
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
    await _send(handle, "single", expected_turn=1)
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
    await _send(handle, "single", expected_turn=1)
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
    assert status.has_custom_approval_fallback is False

    await _send(handle, "single", expected_turn=1)
    events = await _drain_to_turn_end(client, handle.id)
    assert AgentEventType.TOOL_APPROVAL_REQUESTED not in _types_for(events, "g1")
    assert _reply_text(events) == "act:S"


async def test_custom_fallback_approves_what_policy_did_not(env_and_client):
    """The custom fallback (final layer) auto-approves gated_activity_tool though the
    policy (always_require) did not — so it runs ungated; status reports the fallback."""
    client, task_queue = env_and_client
    handle = await _start(
        client, task_queue, workflow_cls=CustomFallbackProbeAgent
    )
    agent_client = AgentClient(client, handle.id)
    assert (await agent_client.get_status()).has_custom_approval_fallback is True

    await _send(handle, "single", expected_turn=1)
    events = await _drain_to_turn_end(client, handle.id)
    assert _types_for(events, "g1") == [
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    assert _reply_text(events) == "act:S"


# ---------------------------------------------------------------------------
# Runtime relaxation — "approve, and don't ask me about this tool again"
# ---------------------------------------------------------------------------


async def test_remember_allowlists_tool_and_cascades_to_pending(env_and_client):
    """Two concurrent gated calls of the same tool. Approving the FIRST with
    remember=True allow-lists the tool, which auto-resolves the SECOND with no explicit
    decision — and the live policy now lists the tool (so future calls skip the gate)."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "concurrent", expected_turn=1)
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
    await _send(handle, "concurrent", expected_turn=1)
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
    await _send(handle, "single", expected_turn=1)
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


async def test_approval_is_idempotent(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "single", expected_turn=1)
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
    await _send(handle, "concurrent", expected_turn=1)
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
    await _send(handle, "single", expected_turn=1)

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
    await _send(handle, "workflow-tool", expected_turn=1)
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
