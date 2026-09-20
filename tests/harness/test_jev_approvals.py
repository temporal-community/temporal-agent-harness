# ABOUTME: Tests for the builtin Jev-backed auto-approver — the harness primitive that
# lets a System One model decide approve / deny / escalate for a gated tool call.
#
# Two layers, tested apart because they fail for different reasons:
#   * the QUESTION (build_request): pure, so the exact state and criteria put to Jev are
#     asserted without a worker, a workflow, or a TypeSafe key;
#   * the COMPOSITION (decide): pure, so every threshold path is asserted exhaustively —
#     and every one of them that is not a confident verdict lands on ESCALATE, which is
#     the whole safety argument;
# plus the worker-side wiring: the activity is registered by the plugin, and a worker
# missing the optional `jev` extra fails it non-retryably (so the call escalates) rather
# than leaving the name unregistered (which Temporal retries forever).
#
# Nothing here calls TypeSafe. The one place a live model would sit — the activity body —
# is exercised against a fake client, because what needs testing is the projection of its
# answers into JevApprovalAnswer, not the model's judgment.
#
# Run with: uv run pytest tests/harness/test_jev_approvals.py -v

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
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
    AutoApprovalContext,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.jev_approvals import (
    DEFAULT_JEV_MODEL,
    JEV_TOOL_APPROVAL_ACTIVITY,
    JevApprovalAnswer,
    JevApprovalRequest,
    build_request,
    decide,
    jev_evaluator,
)


def _ctx(**kwargs) -> AutoApprovalContext:
    return AutoApprovalContext(
        tool_name=kwargs.pop("tool_name", "delete_account"),
        tool_input=kwargs.pop("tool_input", {"account_id": "acct_9"}),
        inherently_safe=kwargs.pop("inherently_safe", False),
        tool_description=kwargs.pop(
            "tool_description", "Permanently delete a customer account."
        ),
        **kwargs,
    )


def _answer(**kwargs) -> JevApprovalAnswer:
    return JevApprovalAnswer(
        model=kwargs.pop("model", "jev-1.0"),
        request_id=kwargs.pop("request_id", "req_1"),
        verdict=kwargs.pop("verdict", "approve"),
        verdict_confidence=kwargs.pop("verdict_confidence", 0.95),
        verdict_probabilities=kwargs.pop("verdict_probabilities", {}),
        irreversible=kwargs.pop("irreversible", 0.02),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The question put to Jev
# ---------------------------------------------------------------------------


def test_the_request_shows_jev_the_call_and_the_operators_rules():
    """The state is the whole basis of the decision, so it carries the operator's policy,
    what the tool does (its docstring — the same prose the model saw when it chose the
    call), and the exact arguments it would run with."""
    request = build_request(_ctx(), policy="Deny deletions. Escalate anything in prod.")

    assert request.model == DEFAULT_JEV_MODEL
    assert request.state["approval_policy"] == "Deny deletions. Escalate anything in prod."
    assert request.state["tool"] == {
        "name": "delete_account",
        "description": "Permanently delete a customer account.",
        "declared_inherently_safe": False,
    }
    assert request.state["call_arguments"] == {"account_id": "acct_9"}
    # No developer context supplied, so the key is absent rather than empty — an empty
    # object would read to the model as "there is context, and it says nothing".
    assert "context" not in request.state


def test_extra_state_is_accepted_as_a_mapping_or_a_callable():
    """The policy usually turns on facts the harness does not know — who is acting, which
    environment. A callable form lets those be read per call, off the context."""
    fixed = build_request(_ctx(), policy="p", extra_state={"env": "staging"})
    assert fixed.state["context"] == {"env": "staging"}

    dynamic = build_request(
        _ctx(tool_name="wipe"),
        policy="p",
        extra_state=None,
    )
    assert "context" not in dynamic.state

    approver = jev_evaluator(policy="p", extra_state=lambda ctx: {"tool": ctx.tool_name})
    assert callable(approver)


def test_the_choice_offers_exactly_the_three_verdicts():
    """The model cannot answer outside the offered labels, so the three-way outcome is
    guaranteed by construction rather than by parsing."""
    request = build_request(_ctx(), policy="p")
    verdict = request.questions["verdict"]
    assert verdict["type"] == "choice"
    assert set(verdict["criteria"]) == {"approve", "deny", "escalate"}
    # Each option says what it is FOR and what it is NOT for, which is what keeps
    # "the policy is silent" from collapsing into "approve".
    for option in verdict["criteria"].values():
        assert set(option) == {"what", "when", "not_for"}


def test_irreversibility_is_asked_separately_in_the_same_request():
    """A second, independent judgment over the same state: TypeSafe answers both in
    parallel, so it costs no extra round-trip, and it stays reusable — code decides what
    to do with it (see decide), the model does not."""
    request = build_request(_ctx(), policy="p")
    assert set(request.questions) == {"verdict", "irreversible"}
    assert request.questions["irreversible"]["type"] == "noul"


# ---------------------------------------------------------------------------
# Composition — thresholds, and every unsure path ending at a human
# ---------------------------------------------------------------------------


def test_a_confident_approve_of_a_reversible_call_is_approved():
    decision = decide(_answer(verdict="approve", verdict_confidence=0.95, irreversible=0.02))
    assert decision.verdict is AutoApprovalVerdict.APPROVE
    # The reason is the audit trail for a decision no human made, so it names the model
    # that made it and the numbers it made it on.
    assert "jev/jev-1.0" in decision.reason
    assert "95%" in decision.reason


def test_a_confident_deny_is_denied():
    decision = decide(_answer(verdict="deny", verdict_confidence=0.9))
    assert decision.verdict is AutoApprovalVerdict.DENY
    assert "forbids" in decision.reason


def test_an_explicit_escalate_goes_to_a_human():
    decision = decide(_answer(verdict="escalate", verdict_confidence=0.9))
    assert decision.verdict is AutoApprovalVerdict.ESCALATE


@pytest.mark.parametrize("verdict", ["approve", "deny", "escalate"])
def test_low_confidence_escalates_whatever_the_verdict(verdict):
    """Confidence is checked BEFORE the label is read. A spread-out distribution is not an
    answer, and that includes a refusal — an unsure deny is still an unsure decision."""
    decision = decide(
        _answer(verdict=verdict, verdict_confidence=0.6), min_confidence=0.8
    )
    assert decision.verdict is AutoApprovalVerdict.ESCALATE
    assert "below the 80% confidence" in decision.reason


def test_an_irreversible_call_is_escalated_even_when_the_policy_allows_it():
    """The one place the harness overrides a confident approve: a mistake that cannot be
    taken back is worth a person's two seconds."""
    decision = decide(
        _answer(verdict="approve", verdict_confidence=0.99, irreversible=0.8),
        escalate_if_irreversible_above=0.5,
    )
    assert decision.verdict is AutoApprovalVerdict.ESCALATE
    assert "irreversible" in decision.reason


def test_the_irreversibility_ceiling_can_be_turned_off():
    decision = decide(
        _answer(verdict="approve", verdict_confidence=0.99, irreversible=0.99),
        escalate_if_irreversible_above=None,
    )
    assert decision.verdict is AutoApprovalVerdict.APPROVE


def test_irreversibility_never_rescues_a_deny_or_creates_an_approve():
    """The ceiling gates APPROVE only — it cannot flip a refusal, and a call that looks
    perfectly safe still cannot be auto-approved if the policy said no."""
    assert (
        decide(_answer(verdict="deny", verdict_confidence=0.95, irreversible=0.0)).verdict
        is AutoApprovalVerdict.DENY
    )
    assert (
        decide(_answer(verdict="escalate", verdict_confidence=0.95, irreversible=0.0)).verdict
        is AutoApprovalVerdict.ESCALATE
    )


def test_an_unrecognized_verdict_escalates():
    """Unreachable through a Choice, which can only return an offered label — asserted
    anyway because the one acceptable failure mode for an approval gate is asking a
    human too often."""
    decision = decide(_answer(verdict="maybe", verdict_confidence=0.99))
    assert decision.verdict is AutoApprovalVerdict.ESCALATE


# ---------------------------------------------------------------------------
# Worker-side wiring
# ---------------------------------------------------------------------------


def test_the_plugin_registers_the_approval_activity_unconditionally():
    """Registered on every worker, extra or not: the workflow dispatches this activity by
    NAME, and an unregistered name is a RETRYABLE Temporal error, which would hang every
    gated tool call mid-approval instead of failing it once."""
    from temporal_agent_harness.plugin import AgentHarnessPlugin

    names = [
        getattr(a, "__temporal_activity_definition").name
        for a in AgentHarnessPlugin()._worker_activities
    ]
    assert JEV_TOOL_APPROVAL_ACTIVITY in names


def test_a_worker_without_the_extra_fails_the_check_non_retryably(monkeypatch):
    """So the approver turns it into an escalate and a human still resolves the gate."""
    import builtins

    from temporal_agent_harness.harness.jev_approvals.activity import (
        JEV_MISSING_EXTRA_ERROR,
        _require_jev_extra,
    )

    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "typesafe_sdk":
            raise ImportError("no typesafe_sdk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(ApplicationError) as excinfo:
        _require_jev_extra()
    assert excinfo.value.type == JEV_MISSING_EXTRA_ERROR
    assert excinfo.value.non_retryable is True
    assert "temporal-agent-harness[jev]" in str(excinfo.value)


async def test_the_activity_projects_typesafe_answers_into_the_typed_result(monkeypatch):
    """The activity's only real job: ferry the workflow-composed questions and flatten the
    SDK's answers into the model the workflow reads."""
    from types import SimpleNamespace

    from temporal_agent_harness.harness.jev_approvals import activity as activity_mod

    seen: dict = {}

    class FakeClient:
        async def system_one(self, state, questions, model=None):
            seen.update(state=state, questions=questions, model=model)
            return SimpleNamespace(
                model="jev-1.0",
                request_id="req_42",
                usage=SimpleNamespace(input_tokens=11, output_tokens=2),
                choices={
                    "verdict": SimpleNamespace(
                        choice="deny",
                        confidence=0.91,
                        probabilities={"approve": 0.04, "deny": 0.91, "escalate": 0.05},
                    )
                },
                nouls={"irreversible": SimpleNamespace(noul=0.77)},
            )

    monkeypatch.setattr(activity_mod, "_require_jev_extra", lambda: None)
    monkeypatch.setattr(activity_mod, "_typesafe_client", FakeClient)

    request = build_request(_ctx(), policy="Deny deletions.")
    answer = await activity_mod.jev_tool_approval(request)

    # Asked exactly what the workflow composed — the activity adds no prompt of its own,
    # which is what makes the activity's INPUT in history the whole audit record.
    assert seen["state"] == request.state
    assert seen["questions"] == request.questions
    assert seen["model"] == DEFAULT_JEV_MODEL

    assert answer.model == "jev-1.0"
    assert answer.request_id == "req_42"
    assert (answer.verdict, answer.verdict_confidence) == ("deny", 0.91)
    assert answer.verdict_probabilities["deny"] == 0.91
    assert answer.irreversible == 0.77
    assert (answer.input_tokens, answer.output_tokens) == (11, 2)

    # End to end through the composition: a confident deny is a denial.
    assert decide(answer).verdict is AutoApprovalVerdict.DENY


# ---------------------------------------------------------------------------
# End to end — the approver driving a real gate in a real workflow
# ---------------------------------------------------------------------------
#
# The Jev ACTIVITY is stood in for (a live model in a test would be neither cheap nor
# deterministic), but everything around it is real: the workflow-side approver, dispatch of
# the activity BY NAME, the composition, the approval gate, and the published events. What
# this proves is the wiring that the pure tests above cannot — that a value returned from
# jev_evaluator() is a working auto_approval_evaluator.


@agent.activity_tool_defn()
async def refund_customer(amount: int) -> str:
    """Refund a customer. Moves real money."""
    return f"refunded:{amount}"


@workflow.defn
@agent.defn
class JevApprovalProbeAgent:
    """Gates everything, and hands every gated call to the Jev auto-approver."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
            auto_approval_evaluator=jev_evaluator(
                policy="Refunds under 50 are routine. Never refund more than that.",
                extra_state={"environment": "test"},
            ),
        )

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Try one refund of the amount named in the message."""
        try:
            result = await self._runner.run_tool(
                "r1", refund_customer, int(message.text)
            )
        except agent.ToolApprovalDenied as e:
            result = f"denied:{e.reason}"
        return TextReply(text=result)

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)


def _fake_jev(verdict: str, confidence: float, irreversible: float):
    """A stand-in Jev activity, registered under the real activity name.

    It also captures the request, so the test can assert the workflow really did compose
    the full question (policy, tool docstring, arguments, developer context) and hand it
    over — the activity input is the audit record, and it is only worth anything if it is
    complete.
    """
    seen: list[JevApprovalRequest] = []

    @activity.defn(name=JEV_TOOL_APPROVAL_ACTIVITY)
    async def fake(request: JevApprovalRequest) -> JevApprovalAnswer:
        seen.append(request)
        return JevApprovalAnswer(
            model="jev-test",
            request_id="req_e2e",
            verdict=verdict,
            verdict_confidence=confidence,
            verdict_probabilities={verdict: confidence},
            irreversible=irreversible,
        )

    return fake, seen


@asynccontextmanager
async def _jev_env(fake_activity):
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"jev-approval-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[JevApprovalProbeAgent],
        activities=[agent.tool_activity(refund_customer), fake_activity],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _run_once(client, task_queue: str, amount: str) -> list[AgentEvent]:
    handle = await client.start_workflow(
        JevApprovalProbeAgent.run,
        AgentConfig(),
        id=f"jev-probe-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="act", payload={"text": amount}),
        result_type=AgentMessageReply,
    )
    events: list[AgentEvent] = []
    stream = WorkflowStreamClient.create(client, handle.id)
    async with asyncio.timeout(30):
        async for item in stream.subscribe(
            topics=[TURN_EVENTS_TOPIC],
            from_offset=0,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            events.append(item.data)
            if item.data.event.type == AgentEventType.TURN_END:
                break
    return events


def _tool_event_types(events: list[AgentEvent]) -> list[str]:
    return [e.event.type for e in events if getattr(e.event, "tool_id", None) == "r1"]


def _reply(events: list[AgentEvent]) -> str:
    end = next(e.event for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END)
    return end.output["text"]


def _one(events: list[AgentEvent], event_type: str):
    matches = [e.event for e in events if e.event.type == event_type]
    assert len(matches) == 1, f"expected exactly one {event_type}, got {len(matches)}"
    return matches[0]


async def test_end_to_end_approve_dispatches_the_tool_with_no_human():
    fake, seen = _fake_jev("approve", confidence=0.95, irreversible=0.1)
    async with _jev_env(fake) as (client, task_queue):
        events = await _run_once(client, task_queue, "20")

    # The question the workflow actually asked, as recorded in the activity's input.
    assert len(seen) == 1
    state = seen[0].state
    assert state["approval_policy"].startswith("Refunds under 50")
    assert state["tool"]["description"] == "Refund a customer. Moves real money."
    assert state["call_arguments"] == {"amount": 20}
    assert state["context"] == {"environment": "test"}

    assert _tool_event_types(events) == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    started = _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED)
    ended = _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert started.evaluator == ended.evaluator == "jev_evaluator"
    assert started.evaluation_id == ended.evaluation_id
    assert ended.verdict is AutoApprovalVerdict.APPROVE

    # The structured record of WHY, on the stream and not only in prose: the raw judgments
    # and the thresholds applied to them, which together are enough to re-derive the
    # verdict — or to re-read this decision under different thresholds later without
    # asking the model again.
    assert ended.details == {
        "model": "jev-test",
        "request_id": "req_e2e",
        "verdict": "approve",
        "confidence": 0.95,
        "probabilities": {"approve": 0.95},
        "irreversible": 0.1,
        "usage": {"input_tokens": None, "output_tokens": None},
        "thresholds": {"min_confidence": 0.8, "escalate_if_irreversible_above": 0.5},
    }

    resolved = _one(events, AgentEventType.TOOL_APPROVAL_RESOLVED)
    assert resolved.approved is True
    assert "jev/jev-test" in resolved.reason
    assert _reply(events) == "refunded:20"


async def test_end_to_end_deny_stops_the_call_and_tells_the_model_why():
    fake, _seen = _fake_jev("deny", confidence=0.92, irreversible=0.9)
    async with _jev_env(fake) as (client, task_queue):
        events = await _run_once(client, task_queue, "500")

    # No tool_start: the activity was never dispatched.
    assert _tool_event_types(events) == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
    ]
    ended = _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.verdict is AutoApprovalVerdict.DENY
    assert ended.details["irreversible"] == 0.9
    resolved = _one(events, AgentEventType.TOOL_APPROVAL_RESOLVED)
    assert resolved.approved is False
    # The reason reaches the model as the error, so it can try something allowed instead.
    assert _reply(events).startswith("denied:jev/jev-test")


async def test_end_to_end_an_unsure_verdict_leaves_the_call_to_a_human():
    """The escalate path is the one that matters most: the auto-approver declining to
    decide must leave the gate exactly where it would have been with no approver at all."""
    fake, _seen = _fake_jev("approve", confidence=0.4, irreversible=0.1)
    async with _jev_env(fake) as (client, task_queue):
        handle = await client.start_workflow(
            JevApprovalProbeAgent.run,
            AgentConfig(),
            id=f"jev-probe-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        await handle.execute_update(
            SEND_AGENT_MESSAGE_UPDATE,
            AgentMessage(type="act", payload={"text": "20"}),
            result_type=AgentMessageReply,
        )
        agent_client = AgentClient(client, handle.id)

        events: list[AgentEvent] = []
        stream = WorkflowStreamClient.create(client, handle.id)
        async with asyncio.timeout(30):
            async for item in stream.subscribe(
                topics=[TURN_EVENTS_TOPIC],
                from_offset=0,
                result_type=AgentEvent,
                poll_cooldown=timedelta(milliseconds=10),
            ):
                ev = item.data
                events.append(ev)
                if (
                    ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                    and ev.event.tool_id == "r1"
                ):
                    await agent_client.approve_tool("r1", approved=False, reason="not today")
                if ev.event.type == AgentEventType.TURN_END:
                    break

    resolved = _one(events, AgentEventType.TOOL_APPROVAL_RESOLVED)
    # The HUMAN's decision, not the approver's — it never resolved the entry.
    assert (resolved.approved, resolved.reason) == (False, "not today")
    assert _reply(events) == "denied:not today"

    # …but the escalate is on the stream in its own right, with the number that caused it.
    # This is the case that had NO record at all before the bracket existed: "why is this
    # in my approval queue?" is now answerable without re-running anything.
    ended = _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.verdict is AutoApprovalVerdict.ESCALATE
    assert ended.details["confidence"] == 0.4
    assert ended.details["thresholds"]["min_confidence"] == 0.8
    assert "below the 80% confidence" in ended.reason


async def test_end_to_end_a_failing_jev_call_escalates_rather_than_approving():
    """The approver must not fail open when TypeSafe cannot be reached.

    A TypeSafe outage is an evaluation that FAILED, not one that abstained, so the bracket
    closes on ``auto_approval_evaluation_error`` carrying the failure — and the harness
    substitutes an escalate, so a human still resolves the gate."""

    @activity.defn(name=JEV_TOOL_APPROVAL_ACTIVITY)
    async def broken(request: JevApprovalRequest) -> JevApprovalAnswer:
        raise ApplicationError("typesafe is down", non_retryable=True)

    async with _jev_env(broken) as (client, task_queue):
        handle = await client.start_workflow(
            JevApprovalProbeAgent.run,
            AgentConfig(),
            id=f"jev-probe-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        await handle.execute_update(
            SEND_AGENT_MESSAGE_UPDATE,
            AgentMessage(type="act", payload={"text": "20"}),
            result_type=AgentMessageReply,
        )
        agent_client = AgentClient(client, handle.id)

        events: list[AgentEvent] = []
        stream = WorkflowStreamClient.create(client, handle.id)
        async with asyncio.timeout(30):
            async for item in stream.subscribe(
                topics=[TURN_EVENTS_TOPIC],
                from_offset=0,
                result_type=AgentEvent,
                poll_cooldown=timedelta(milliseconds=10),
            ):
                ev = item.data
                events.append(ev)
                if (
                    ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                    and ev.event.tool_id == "r1"
                ):
                    await agent_client.approve_tool("r1", approved=True, reason="ok by me")
                if ev.event.type == AgentEventType.TURN_END:
                    break

    # It ran only because a HUMAN said so.
    assert _tool_event_types(events) == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED,
        AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    started = _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_STARTED)
    failed = _one(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    assert started.evaluation_id == failed.evaluation_id
    assert failed.evaluator == "jev_evaluator"
    assert "typesafe is down" in failed.message
    resolved = _one(events, AgentEventType.TOOL_APPROVAL_RESOLVED)
    assert (resolved.approved, resolved.reason) == (True, "ok by me")
    assert _reply(events) == "refunded:20"
