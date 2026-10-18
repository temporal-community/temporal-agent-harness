# ABOUTME: Tests for the Tenuo auto mode evaluator — the evaluator that checks a gated tool
# call against the session's warrant, and gets a gated call approved by the approvers the
# warrant names, through Tenuo's approval handlers, never through the harness's approval UI.
#
# Two layers:
#   * the DECISION (decide): pure apart from the time it is handed, so every path through
#     the warrant check, with and without signed approvals, is asserted without a workflow;
#   * end-to-end runs through a real gate in a real workflow, with the warrant arriving in
#     the session's init data and approvals coming from a Tenuo approval handler on the worker.
#
# Run with: uv run pytest tests/ai_sdks/tenuo/test_tenuo_evaluator.py -v

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta

import pytest
from pydantic import BaseModel
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

tenuo = pytest.importorskip("tenuo")
from tenuo import Authorizer, Exact, Range, SigningKey, Warrant  # noqa: E402
from tenuo.approval import ApprovalDenied, cli_prompt, sign_approval  # noqa: E402
from tenuo.temporal import TenuoPluginConfig  # noqa: E402

from temporal_agent_harness.ai_sdks.tenuo import (  # noqa: E402
    WARRANT_CRITERIA_SET,
    TenuoApprovalActivities,
    approval_request,
    build_authorizer,
    decide,
    tenuo_evaluator,
    warrant_criteria,
)
from temporal_agent_harness.harness import AgentWorkflowRunner, agent  # noqa: E402
from temporal_agent_harness.harness.agent_client import AgentClient  # noqa: E402
from temporal_agent_harness.harness.agent_protocol import (  # noqa: E402
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    AutoApprovalVerdict,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)

ROOT = SigningKey.generate()
HOLDER = SigningKey.generate()
APPROVER = SigningKey.generate()  # the support lead the warrant names
STRANGER = SigningKey.generate()  # anyone else


class _Approver:
    """A Tenuo approval handler whose behaviour each test picks."""

    def __init__(self) -> None:
        self.mode = "approve"  # approve | stranger | refuse | waiting | waits_then_approves
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        if self.mode == "stranger":
            return sign_approval(request, STRANGER, external_id="stranger")
        if self.mode == "refuse":
            raise ApprovalDenied(request, reason="refused by lead")
        if self.mode == "waiting" or (self.mode == "waits_then_approves" and self.calls < 3):
            # How Tenuo Cloud's Temporal handler says "nobody has decided yet": a retryable
            # error, so Temporal retries the activity instead of holding a worker slot.
            raise ApplicationError("approval pending", non_retryable=False)
        return sign_approval(request, APPROVER, external_id="lead")


APPROVER_HANDLER = _Approver()
# One config, as a worker would build it: the same object the plugin would use.
CONFIG = TenuoPluginConfig(
    signing_key=HOLDER, trusted_roots=[ROOT.public_key], approval_handler=APPROVER_HANDLER
)
MAPPED_CONFIG = TenuoPluginConfig(
    signing_key=HOLDER, trusted_roots=[ROOT.public_key], tool_mappings={"issue_refund": "refund"}
)
with pytest.warns(UserWarning):  # dry_run warns that it doesn't enforce, by design
    OBSERVE_CONFIG = TenuoPluginConfig(
        signing_key=HOLDER, trusted_roots=[ROOT.public_key], dry_run=True
    )


def _warrant(*, ttl: int = 600, root: SigningKey = ROOT, holder: SigningKey = HOLDER) -> Warrant:
    """Refunds on one order, up to 500. Over 100 needs the support lead's signed approval."""
    return (
        Warrant.mint_builder()
        .capability("refund", order_id=Exact("A-1"), amount=Range(1, 500))
        .approval_gates({"refund": {"amount": {"exempt": Range(1, 100)}}})
        .required_approvers([APPROVER.public_key])
        .min_approvals(1)
        .holder(holder.public_key)
        .ttl(ttl)
        .mint(root)
    )


def _signed(warrant: Warrant, args: dict, *, by: SigningKey = APPROVER):
    return sign_approval(
        approval_request(warrant, "refund", args), by, external_id="lead", ttl_seconds=300
    )


def _decide(args, *, tool="refund", warrant=None, now=None, roots=(ROOT,), approvals=None):
    warrant = warrant or _warrant()
    return decide(
        [warrant],
        tool,
        args,
        authorizer=Authorizer(trusted_roots=[r.public_key for r in roots]),
        signing_key=HOLDER,
        now=int(time.time()) if now is None else now,
        approvals=approvals,
    )


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def test_a_call_inside_the_warrant_is_approved():
    decision = _decide({"order_id": "A-1", "amount": 40})
    assert decision.verdict is AutoApprovalVerdict.APPROVE
    assert decision.details["status"] == "exempt"
    assert decision.details["tool"] == "refund"


@pytest.mark.parametrize(
    ("args", "named", "value"),
    [
        ({"order_id": "B-2", "amount": 40}, "order_id", "'B-2'"),  # another order
        ({"order_id": "A-1", "amount": 900}, "amount", "900"),  # over the warrant's ceiling
        ({"order_id": "A-1", "amount": 40, "note": "hi"}, "note", "'hi'"),  # an argument it doesn't name
    ],
)
def test_a_call_outside_the_warrant_is_denied_and_says_which_argument(args, named, value):
    decision = _decide(args)
    assert decision.verdict is AutoApprovalVerdict.DENY
    # The reason is the error the model sees, so it has to name what was wrong, and the value.
    assert f"{named} {value}" in decision.reason
    assert decision.details["code"] == "constraint-violation"


def test_a_tool_the_warrant_does_not_grant_is_denied():
    decision = _decide({"account_id": "x"}, tool="delete_account")
    assert decision.verdict is AutoApprovalVerdict.DENY
    assert "delete_account" in decision.reason
    assert decision.details["code"] == "tool-not-authorized"


def test_a_gated_call_without_approvals_needs_the_warrant_s_approvers():
    # decide() reports this as ESCALATE; tenuo_evaluator turns it into an approval request
    # and never returns it (see the end-to-end tests).
    decision = _decide({"order_id": "A-1", "amount": 300})
    assert decision.verdict is AutoApprovalVerdict.ESCALATE
    assert decision.details["status"] == "required"
    assert decision.details["argument"] == "amount"


def test_a_gated_call_with_its_approver_s_signature_is_approved():
    args = {"order_id": "A-1", "amount": 300}
    w = _warrant()
    decision = _decide(args, warrant=w, approvals=[_signed(w, args)])
    assert decision.verdict is AutoApprovalVerdict.APPROVE
    assert decision.details["approvals"] == 1


def test_an_approval_from_someone_the_warrant_does_not_name_is_denied():
    args = {"order_id": "A-1", "amount": 300}
    w = _warrant()
    decision = _decide(args, warrant=w, approvals=[_signed(w, args, by=STRANGER)])
    assert decision.verdict is AutoApprovalVerdict.DENY
    assert "approver" in decision.reason


def test_an_approval_signed_for_different_arguments_is_denied():
    w = _warrant()
    signed_for_300 = _signed(w, {"order_id": "A-1", "amount": 300})
    decision = _decide({"order_id": "A-1", "amount": 400}, warrant=w, approvals=[signed_for_300])
    assert decision.verdict is AutoApprovalVerdict.DENY


def test_a_gated_call_outside_the_warrant_is_denied_not_sent_for_approval():
    # Nobody must be asked to approve something the warrant would refuse anyway.
    decision = _decide({"order_id": "B-2", "amount": 300})
    assert decision.verdict is AutoApprovalVerdict.DENY


def test_an_expired_warrant_denies_even_an_allowed_call():
    now = int(time.time())
    decision = _decide({"order_id": "A-1", "amount": 40}, warrant=_warrant(ttl=60), now=now + 120)
    assert decision.verdict is AutoApprovalVerdict.DENY
    assert "expired" in decision.reason


def test_the_verdict_depends_on_the_time_it_is_given_not_the_clock():
    # The same call, checked "as of" two times either side of expiry. This is what makes the
    # evaluator replay-safe: it is handed workflow.now(), never the wall clock.
    w, now = _warrant(ttl=60), int(time.time())
    args = {"order_id": "A-1", "amount": 40}
    assert _decide(args, warrant=w, now=now + 30).verdict is AutoApprovalVerdict.APPROVE
    assert _decide(args, warrant=w, now=now + 90).verdict is AutoApprovalVerdict.DENY


@pytest.mark.parametrize("problem", ["expired", "untrusted"])
def test_a_gated_call_on_an_invalid_warrant_is_denied_not_sent_for_approval(problem):
    # An expired or untrusted warrant allows nothing, so nobody is asked to approve what it
    # gates either.
    now = int(time.time())
    args = {"order_id": "A-1", "amount": 300}
    if problem == "expired":
        decision = _decide(args, warrant=_warrant(ttl=60), now=now + 120)
    else:
        decision = _decide(args, roots=(SigningKey.generate(),))
    assert decision.verdict is AutoApprovalVerdict.DENY
    assert "not valid" in decision.reason


def test_the_authorizer_trusts_the_config_s_roots():
    authorizer = build_authorizer(CONFIG)
    assert authorizer.verify_chain([_warrant()], as_of=int(time.time())) is not None
    with pytest.raises(Exception):
        build_authorizer(CONFIG).verify_chain(
            [_warrant(root=SigningKey.generate())], as_of=int(time.time())
        )


def test_observe_mode_follows_tenuo_s_global_mode_or_the_config(monkeypatch):
    from temporal_agent_harness.ai_sdks.tenuo import evaluator

    assert evaluator.observe_mode(CONFIG) is False
    assert evaluator.observe_mode(OBSERVE_CONFIG) is True  # dry_run
    monkeypatch.setattr(evaluator, "is_enforce_mode", lambda: False)  # configure(mode="audit")
    assert evaluator.observe_mode(CONFIG) is True


def test_the_holder_key_must_match_the_warrant():
    other = TenuoPluginConfig(signing_key=SigningKey.generate(), trusted_roots=[ROOT.public_key])
    with pytest.raises(ValueError, match="not the holder"):
        tenuo_evaluator(warrant=_warrant(), config=other)


def test_the_evaluator_takes_a_warrant_in_any_of_its_forms():
    w = _warrant()
    for form in (w, w.to_base64(), [w.to_base64()]):
        tenuo_evaluator(warrant=form, config=CONFIG)
    with pytest.raises(ValueError):
        tenuo_evaluator(warrant=[], config=CONFIG)


def test_the_criteria_put_every_tool_in_front_of_the_warrant():
    criteria = warrant_criteria()
    assert criteria.set_name_for("refund") == WARRANT_CRITERIA_SET
    assert criteria.set_name_for("anything_else") == WARRANT_CRITERIA_SET


# ---------------------------------------------------------------------------
# End to end — the evaluator driving a real gate in a real workflow
# ---------------------------------------------------------------------------


@agent.activity_tool_defn()
async def refund(order_id: str, amount: int) -> str:
    """Refund part of an order. Moves real money."""
    return f"refunded:{order_id}:{amount}"


@agent.activity_tool_defn()
async def issue_refund(order_id: str, amount: int) -> str:
    """The same refund under the harness name a different codebase gave it."""
    return f"refunded:{order_id}:{amount}"


class RefundTask(BaseModel):
    """The session's init data: the warrant issued for this task."""

    warrant: str


def _runner(
    config: AgentConfig, task: RefundTask, tenuo_config, stream, *, confirm=()
) -> AgentWorkflowRunner:
    """Gates everything, with auto mode on, so every call goes to the warrant. (The stream is
    built by the caller: WorkflowStream must be constructed in the workflow's own __init__.)"""
    return AgentWorkflowRunner(
        config,
        stream=stream,
        approval_policy_default=ToolApprovalPolicy.auto_mode(),
        auto_approval_criteria_default=warrant_criteria(),
        auto_mode_evaluator=tenuo_evaluator(
            warrant=task.warrant, config=tenuo_config, confirm=confirm
        ),
    )


async def _act(runner: AgentWorkflowRunner, tool, message: TextMessage) -> TextReply:
    """Try one refund: the message is `<order_id> <amount>`."""
    order_id, amount = message.text.split()
    try:
        result = await runner.run_tool("r1", tool, order_id, int(amount))
    except agent.ToolApprovalDenied as e:
        result = f"denied:{e.reason}"
    return TextReply(text=result)


@agent.defn
class TenuoApprovalProbeAgent:
    @agent.init
    def __init__(self, config: AgentConfig, task: RefundTask) -> None:
        self._runner = _runner(config, task, CONFIG, WorkflowStream())

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Try one refund."""
        return await _act(self._runner, refund, message)


@agent.defn
class TenuoMappedToolProbeAgent:
    """Its tool is `issue_refund`; the warrant grants `refund`; tool_mappings maps them."""

    @agent.init
    def __init__(self, config: AgentConfig, task: RefundTask) -> None:
        self._runner = _runner(config, task, MAPPED_CONFIG, WorkflowStream())

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Try one refund."""
        return await _act(self._runner, issue_refund, message)


@agent.defn
class TenuoObserveProbeAgent:
    """Observe mode (dry_run): everything is checked, nothing is blocked."""

    @agent.init
    def __init__(self, config: AgentConfig, task: RefundTask) -> None:
        self._runner = _runner(config, task, OBSERVE_CONFIG, WorkflowStream())

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Try one refund."""
        return await _act(self._runner, refund, message)


@agent.defn
class TenuoConfirmProbeAgent:
    """confirm=["refund"]: refunds the warrant allows still wait for a person in the UI."""

    @agent.init
    def __init__(self, config: AgentConfig, task: RefundTask) -> None:
        self._runner = _runner(config, task, CONFIG, WorkflowStream(), confirm=["refund"])

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Try one refund."""
        return await _act(self._runner, refund, message)


@agent.defn
class TenuoConfirmAboveProbeAgent:
    """confirm as a function: refunds over 50 wait for a person; smaller ones run."""

    @agent.init
    def __init__(self, config: AgentConfig, task: RefundTask) -> None:
        self._runner = _runner(
            config, task, CONFIG, WorkflowStream(),
            confirm=lambda tool, args: tool == "refund" and args["amount"] > 50,
        )

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Try one refund."""
        return await _act(self._runner, refund, message)


@agent.defn
class TenuoConfirmRaisesProbeAgent:
    """A confirm function that raises: the call is put to a person."""

    @agent.init
    def __init__(self, config: AgentConfig, task: RefundTask) -> None:
        self._runner = _runner(
            config, task, CONFIG, WorkflowStream(), confirm=lambda tool, args: args["missing"]
        )

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Try one refund."""
        return await _act(self._runner, refund, message)


@agent.defn
class TenuoConfirmMappedProbeAgent:
    """Its tool is `issue_refund`; confirm names the warrant's `refund`."""

    @agent.init
    def __init__(self, config: AgentConfig, task: RefundTask) -> None:
        self._runner = _runner(config, task, MAPPED_CONFIG, WorkflowStream(), confirm=["refund"])

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Try one refund."""
        return await _act(self._runner, issue_refund, message)


PROBES = [
    TenuoApprovalProbeAgent,
    TenuoMappedToolProbeAgent,
    TenuoObserveProbeAgent,
    TenuoConfirmProbeAgent,
    TenuoConfirmAboveProbeAgent,
    TenuoConfirmRaisesProbeAgent,
    TenuoConfirmMappedProbeAgent,
]


async def _start(
    client, task_queue: str, text: str, *, warrant: Warrant | None = None, agent_cls=None
):
    handle = await client.start_workflow(
        (agent_cls or TenuoApprovalProbeAgent).run,
        args=[AgentConfig(), RefundTask(warrant=(warrant or _warrant()).to_base64())],
        id=f"tenuo-probe-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="act", payload={"text": text}),
        result_type=AgentMessageReply,
    )
    return handle


async def _events(client, handle, *, on_event=None) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    stream = WorkflowStreamClient.create(client, handle.id)
    async with asyncio.timeout(60):
        async for item in stream.subscribe(
            topics=[TURN_EVENTS_TOPIC],
            from_offset=0,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            events.append(item.data)
            if on_event is not None:
                await on_event(item.data)
            if item.data.event.type == AgentEventType.TURN_END:
                break
    return events


def _worker(client, task_queue: str, *, approvals: bool = True) -> Worker:
    activities = [agent.tool_activity(refund), agent.tool_activity(issue_refund)]
    if approvals:
        activities += TenuoApprovalActivities(CONFIG).all()
    return Worker(
        client,
        task_queue=task_queue,
        workflows=PROBES,
        activities=activities,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )


@pytest.fixture
async def env():
    APPROVER_HANDLER.mode, APPROVER_HANDLER.calls = "approve", 0
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"tenuo-approval-test-{uuid.uuid4()}"
    async with _worker(env.client, task_queue):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


def _tool_event_types(events: list[AgentEvent]) -> list[str]:
    return [e.event.type for e in events if getattr(e.event, "tool_id", None) == "r1"]


def _one(events: list[AgentEvent], event_type: str):
    matches = [e.event for e in events if e.event.type == event_type]
    assert len(matches) == 1, f"expected exactly one {event_type}, got {len(matches)}"
    return matches[0]


def _reply(events: list[AgentEvent]) -> str:
    end = next(e.event for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END)
    return end.output["text"]


def _verdict(events: list[AgentEvent]) -> AutoApprovalVerdict:
    return _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED).verdict


async def test_end_to_end_an_allowed_call_runs_with_no_human(env):
    client, task_queue = env
    handle = await _start(client, task_queue, "A-1 40")
    events = await _events(client, handle)

    assert _tool_event_types(events) == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    ended = _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.evaluator == "tenuo_evaluator"
    assert ended.verdict is AutoApprovalVerdict.APPROVE
    assert ended.details["tool"] == "refund"
    assert _reply(events) == "refunded:A-1:40"
    assert APPROVER_HANDLER.calls == 0  # nothing to approve


async def test_end_to_end_a_call_outside_the_warrant_never_runs(env):
    client, task_queue = env
    handle = await _start(client, task_queue, "B-2 40")
    events = await _events(client, handle)

    # No tool_start: the activity was never dispatched.
    assert AgentEventType.TOOL_START not in _tool_event_types(events)
    assert _verdict(events) is AutoApprovalVerdict.DENY
    assert _one(events, AgentEventType.TOOL_APPROVAL_RESOLVED).approved is False
    # The reason reaches the model as the error, so it can try something allowed instead.
    reply = _reply(events)
    assert reply.startswith("denied:tenuo:") and "order_id" in reply


@pytest.mark.parametrize("mode", ["approve", "waits_then_approves"])
async def test_end_to_end_a_gated_call_runs_once_the_warrant_s_approver_signs(env, mode):
    client, task_queue = env
    APPROVER_HANDLER.mode = mode
    handle = await _start(client, task_queue, "A-1 300")
    events = await _events(client, handle)

    # Approved by the evaluator on the approver's signature: never escalated to the UI.
    assert _verdict(events) is AutoApprovalVerdict.APPROVE
    assert _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED).details["approvals"] == 1
    assert "signed approval" in _one(events, AgentEventType.TOOL_APPROVAL_RESOLVED).reason
    assert _reply(events) == "refunded:A-1:300"
    if mode == "waits_then_approves":
        assert APPROVER_HANDLER.calls == 3  # two "still waiting", retried by Temporal


async def test_end_to_end_tenuo_s_cli_prompt_handler_works_as_is(env, monkeypatch):
    # Tenuo core's own development handler, unchanged: it asks in the worker's terminal and
    # signs with the approver's key. The terminal answer is the only thing stubbed.
    client, task_queue = env
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    monkeypatch.setattr(CONFIG, "approval_handler", cli_prompt(approver_key=APPROVER))
    handle = await _start(client, task_queue, "A-1 300")
    events = await _events(client, handle)
    assert _verdict(events) is AutoApprovalVerdict.APPROVE
    assert _reply(events) == "refunded:A-1:300"


@pytest.mark.parametrize(
    ("mode", "why"),
    [
        ("stranger", "approver"),  # signed, but not by anyone the warrant names
        ("refuse", "refused by lead"),
        ("waiting", "not approved before the warrant expired"),
    ],
)
async def test_end_to_end_a_gated_call_without_a_valid_approval_is_denied(env, mode, why):
    client, task_queue = env
    APPROVER_HANDLER.mode = mode
    # A short warrant, so "nobody decides" reaches the warrant's expiry within the test.
    handle = await _start(client, task_queue, "A-1 300", warrant=_warrant(ttl=5))
    events = await _events(client, handle)

    assert _verdict(events) is AutoApprovalVerdict.DENY
    assert AgentEventType.TOOL_START not in _tool_event_types(events)
    assert why in _reply(events)


async def test_end_to_end_no_approval_handler_denies(env, monkeypatch):
    client, task_queue = env
    monkeypatch.setattr(CONFIG, "approval_handler", None)
    handle = await _start(client, task_queue, "A-1 300")
    events = await _events(client, handle)
    assert _verdict(events) is AutoApprovalVerdict.DENY
    assert "no approval_handler" in _reply(events)


async def test_end_to_end_missing_approval_activities_deny_rather_than_escalate():
    # A worker without TenuoApprovalActivities: approval can't be requested. Had the evaluator
    # raised, the harness would have escalated the call to its approval UI.
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"tenuo-no-approvals-{uuid.uuid4()}"
    try:
        async with _worker(env.client, task_queue, approvals=False):
            handle = await _start(env.client, task_queue, "A-1 300", warrant=_warrant(ttl=5))
            events = await _events(env.client, handle)
    finally:
        await env.shutdown()
    assert _verdict(events) is AutoApprovalVerdict.DENY
    assert "not approved" in _reply(events)


async def test_end_to_end_observe_mode_checks_everything_and_blocks_nothing(env):
    client, task_queue = env
    handle = await _start(client, task_queue, "B-2 40", agent_cls=TenuoObserveProbeAgent)
    events = await _events(client, handle)
    ended = _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.verdict is AutoApprovalVerdict.APPROVE
    assert ended.details["mode"] == "observe"
    assert "order_id" in ended.details["would_deny"]  # recorded, as enforcement would deny
    assert _reply(events) == "refunded:B-2:40"


async def test_a_harness_ui_approval_sent_while_waiting_still_settles_the_gate():
    # This documents the one gap the evaluator can't close by itself. While it waits for the
    # warrant's approvers, the harness still accepts a tool_approval update for the call, and
    # the first decision wins. Closing it needs a policy under which the harness doesn't accept
    # tool_approval for a call an evaluator is deciding.
    APPROVER_HANDLER.mode, APPROVER_HANDLER.calls = "waiting", 0
    env = await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter)
    task_queue = f"tenuo-race-{uuid.uuid4()}"
    try:
        async with _worker(env.client, task_queue):
            handle = await _start(env.client, task_queue, "A-1 300")
            agent_client = AgentClient(env.client, handle.id)
            clicked = False

            async def approve_in_the_ui(ev: AgentEvent) -> None:
                nonlocal clicked
                if ev.event.type == AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED and not clicked:
                    clicked = True
                    await agent_client.approve_tool("r1", approved=True, reason="clicked approve")

            events = await _events(env.client, handle, on_event=approve_in_the_ui)
    finally:
        await env.shutdown()
    resolved = _one(events, AgentEventType.TOOL_APPROVAL_RESOLVED)
    assert (resolved.approved, resolved.reason) == (True, "clicked approve")
    assert _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_SUPERSEDED) is not None
    assert _reply(events) == "refunded:A-1:300"


async def test_end_to_end_tool_mappings_map_a_harness_tool_onto_the_warrant_s(env):
    client, task_queue = env
    handle = await _start(client, task_queue, "A-1 40", agent_cls=TenuoMappedToolProbeAgent)
    events = await _events(client, handle)
    ended = _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.verdict is AutoApprovalVerdict.APPROVE
    assert ended.details["tool"] == "refund"  # judged under the warrant's name
    assert _reply(events) == "refunded:A-1:40"


async def test_end_to_end_the_session_s_own_warrant_decides(env):
    # Same agent, same call, a different session: its warrant is for another order.
    client, task_queue = env
    other_order = (
        Warrant.mint_builder()
        .capability("refund", order_id=Exact("B-2"), amount=Range(1, 500))
        .holder(HOLDER.public_key)
        .ttl(600)
        .mint(ROOT)
    )
    handle = await _start(client, task_queue, "A-1 40", warrant=other_order)
    events = await _events(client, handle)
    assert _reply(events).startswith("denied:tenuo:")


async def test_replay_after_the_warrant_and_approval_expired_takes_the_same_path(env):
    # The run approves a gated call inside a 3-second warrant, and the replay happens after the
    # warrant (and the approval, which can't outlive it) has expired. Everything is checked as of
    # workflow.now(), which replay reads from history, so the verdict is the same. Had any check
    # read the wall clock, the replay would deny the call, skip the activity the history
    # recorded, and fail as nondeterministic.
    client, task_queue = env
    handle = await _start(client, task_queue, "A-1 300", warrant=_warrant(ttl=3))
    events = await _events(client, handle)
    assert _reply(events) == "refunded:A-1:300"
    history: WorkflowHistory = await handle.fetch_history()

    await asyncio.sleep(4)  # real time: the warrant is now expired
    replayer = Replayer(
        workflows=PROBES,
        data_converter=pydantic_data_converter,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    await replayer.replay_workflow(history)  # raises on nondeterminism


async def _confirm_in_the_ui(client, handle, *, approved: bool):
    """Answer the harness's approval UI once the evaluator has escalated the call."""
    agent_client = AgentClient(client, handle.id)

    async def answer(ev: AgentEvent) -> None:
        if (
            ev.event.type == AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED
            and ev.event.verdict is AutoApprovalVerdict.ESCALATE
        ):
            await agent_client.approve_tool("r1", approved=approved, reason="clicked")

    return await _events(client, handle, on_event=answer)


@pytest.mark.parametrize("approved", [True, False])
async def test_confirm_sends_an_allowed_call_to_the_harness_ui(env, approved):
    client, task_queue = env
    handle = await _start(client, task_queue, "A-1 40", agent_cls=TenuoConfirmProbeAgent)
    events = await _confirm_in_the_ui(client, handle, approved=approved)

    ended = _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.verdict is AutoApprovalVerdict.ESCALATE
    assert ended.details["confirm"] is True
    resolved = _one(events, AgentEventType.TOOL_APPROVAL_RESOLVED)
    assert (resolved.approved, resolved.reason) == (approved, "clicked")
    assert _reply(events) == ("refunded:A-1:40" if approved else "denied:clicked")
    assert APPROVER_HANDLER.calls == 0  # a click, not a signature


async def test_confirm_never_puts_a_call_the_warrant_refuses_in_front_of_a_person(env):
    client, task_queue = env
    handle = await _start(client, task_queue, "B-2 40", agent_cls=TenuoConfirmProbeAgent)
    events = await _confirm_in_the_ui(client, handle, approved=True)

    assert _verdict(events) is AutoApprovalVerdict.DENY
    assert AgentEventType.TOOL_START not in _tool_event_types(events)


async def test_confirm_does_not_replace_a_warrant_s_own_approval_gate(env):
    # The warrant gates refunds over its exemption: that still takes the approver's signature,
    # not a click.
    client, task_queue = env
    handle = await _start(client, task_queue, "A-1 300", agent_cls=TenuoConfirmProbeAgent)
    events = await _confirm_in_the_ui(client, handle, approved=False)

    assert _verdict(events) is AutoApprovalVerdict.APPROVE
    assert APPROVER_HANDLER.calls >= 1
    assert _reply(events) == "refunded:A-1:300"


@pytest.mark.parametrize(("amount", "asks"), [(40, False), (80, True)])
async def test_confirm_as_a_function_asks_only_for_the_calls_it_picks(env, amount, asks):
    client, task_queue = env
    handle = await _start(
        client, task_queue, f"A-1 {amount}", agent_cls=TenuoConfirmAboveProbeAgent
    )
    events = await _confirm_in_the_ui(client, handle, approved=True)

    expected = AutoApprovalVerdict.ESCALATE if asks else AutoApprovalVerdict.APPROVE
    assert _verdict(events) is expected
    assert _reply(events) == f"refunded:A-1:{amount}"


async def test_a_confirm_function_that_raises_asks_a_person(env):
    client, task_queue = env
    handle = await _start(client, task_queue, "A-1 40", agent_cls=TenuoConfirmRaisesProbeAgent)
    events = await _confirm_in_the_ui(client, handle, approved=False)

    assert _verdict(events) is AutoApprovalVerdict.ESCALATE
    assert _reply(events) == "denied:clicked"


async def test_confirm_uses_the_warrant_s_tool_names(env):
    client, task_queue = env
    handle = await _start(client, task_queue, "A-1 40", agent_cls=TenuoConfirmMappedProbeAgent)
    events = await _confirm_in_the_ui(client, handle, approved=True)

    assert _verdict(events) is AutoApprovalVerdict.ESCALATE
    assert _reply(events) == "refunded:A-1:40"
