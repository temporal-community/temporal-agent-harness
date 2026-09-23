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
from temporalio.client import WorkflowUpdateStage
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_workflow import Injected
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    AGENT_STATUS_QUERY,
    AgentStatus,
    AutoApprovalCriteria,
    AutoApprovalCriteriaSet,
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


def _criteria(**kwargs) -> AutoApprovalCriteria:
    """A one-set rulebook with that set as the catch-all, which is the shape most of these
    tests want: rules that resolve for whatever tool is under test."""
    return AutoApprovalCriteria(
        sets={"destructive": AutoApprovalCriteriaSet(**kwargs)},
        default="destructive",
    )


_DEFAULT_CRITERIA = _criteria(
    effect="Permanently destroys the named resource.",
    deny_when=("the call deletes anything",),
    escalate_when=("the call touches production",),
)


def _ctx(**kwargs) -> AutoApprovalContext:
    """A context shaped the way the GATE shapes one.

    The governing set is resolved here, exactly as
    ``AgentWorkflowRunner._auto_mode_context`` resolves it, because that is the only way
    an evaluator ever receives a context: ``criteria_set`` is required and non-optional, so a
    call with no rules cannot be represented at all. Tests for what happens when nothing
    resolves therefore live against the RUNNER (see
    ``test_agent_workflow_runner.py``), not here — there is no evaluator-side behaviour left
    to test, which is the point of the guarantee.
    """
    tool_name = kwargs.pop("tool_name", "delete_account")
    criteria = kwargs.pop("criteria", _DEFAULT_CRITERIA)
    declared = kwargs.pop("declared_criteria_set", None)
    criteria_set = kwargs.pop("criteria_set", None) or criteria.for_tool(
        tool_name, declared=declared
    )
    assert criteria_set is not None, (
        "this helper builds the context the gate would build, and the gate does not build "
        "one when no criteria set resolves"
    )
    return AutoApprovalContext(
        tool_name=tool_name,
        tool_input=kwargs.pop("tool_input", {"account_id": "acct_9"}),
        inherently_safe=kwargs.pop("inherently_safe", False),
        tool_description=kwargs.pop(
            "tool_description", "Permanently delete a customer account."
        ),
        criteria=criteria,
        criteria_set=criteria_set,
        criteria_set_name=kwargs.pop(
            "criteria_set_name", criteria.set_name_for(tool_name, declared=declared) or ""
        ),
        **kwargs,
    )


def _decide(answer, **kwargs):
    """``decide`` with the thresholds tests used to get by default. They are now the
    operator's, read off the governing criteria set, so a caller must state them."""
    kwargs.setdefault("min_confidence", 0.8)
    kwargs.setdefault("escalate_if_irreversible_above", 0.5)
    return decide(answer, **kwargs)


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


def test_the_request_shows_jev_the_call_and_the_governing_criteria():
    """The state is the whole basis of the decision, so it carries the rules of the criteria
    set that governs this tool, what the tool does (its docstring — the same prose the model
    saw when it chose the call), and the exact arguments it would run with."""
    request = build_request(_ctx())

    assert request.model == DEFAULT_JEV_MODEL
    assert request.state["approval_criteria"] == {
        "what_calls_to_this_tool_do": "Permanently destroys the named resource.",
        "deny_when": ["the call deletes anything"],
        "escalate_when": ["the call touches production"],
    }
    assert request.state["tool"] == {
        "name": "delete_account",
        "description": "Permanently delete a customer account.",
        "declared_inherently_safe": False,
    }
    assert request.state["call_arguments"] == {"account_id": "acct_9"}
    # No agent state supplied, so the key is absent rather than empty — an empty object
    # would read to the model as "there is state, and it says nothing".
    assert "agent_state" not in request.state


def test_a_rule_the_operator_did_not_write_is_absent_not_empty():
    """An absent key reads as "the operator said nothing about this", which is the truth and
    is what should push a call toward escalate. An empty list invites the model to read it as
    "nothing is forbidden"."""
    request = build_request(_ctx(criteria=_criteria(effect="Reads a record.")))
    assert request.state["approval_criteria"] == {
        "what_calls_to_this_tool_do": "Reads a record."
    }


def test_the_criteria_operating_context_reaches_the_model():
    """``context`` is the serializable half of "what the rules turn on" — the tenant, the
    environment — and travels with the criteria rather than with the evaluator."""
    criteria = _criteria(effect="e").model_copy(update={"context": {"environment": "prod"}})
    request = build_request(_ctx(criteria=criteria))
    assert request.state["approval_criteria"]["operating_context"] == {"environment": "prod"}


def test_extra_state_is_accepted_as_a_mapping_or_a_callable():
    """Some facts must be read from LIVE agent state per call, which configuration cannot
    carry. A callable form lets those be read off the context."""
    fixed = build_request(_ctx(), extra_state={"env": "staging"})
    assert fixed.state["agent_state"] == {"env": "staging"}

    dynamic = build_request(_ctx(tool_name="wipe"), extra_state=None)
    assert "agent_state" not in dynamic.state

    approver = jev_evaluator(extra_state=lambda ctx: {"tool": ctx.tool_name})
    assert callable(approver)


def test_the_choice_offers_exactly_the_three_verdicts():
    """The model cannot answer outside the offered labels, so the three-way outcome is
    guaranteed by construction rather than by parsing."""
    request = build_request(_ctx())
    verdict = request.questions["verdict"]
    assert verdict["type"] == "choice"
    assert set(verdict["criteria"]) == {"approve", "deny", "escalate"}
    # Each option says what it is FOR and what it is NOT for, which is what keeps
    # "the criteria are silent" from collapsing into "approve".
    for option in verdict["criteria"].values():
        assert set(option) == {"what", "when", "not_for"}


def test_irreversibility_is_asked_separately_in_the_same_request():
    """A second, independent judgment over the same state: TypeSafe answers both in
    parallel, so it costs no extra round-trip, and it stays reusable — code decides what
    to do with it (see decide), the model does not."""
    request = build_request(_ctx())
    assert set(request.questions) == {"verdict", "irreversible"}
    assert request.questions["irreversible"]["type"] == "noul"


# ---------------------------------------------------------------------------
# Criteria resolution — which rules govern a call, and what happens with none
# ---------------------------------------------------------------------------


def test_a_runtime_assignment_outranks_the_tools_own_declared_set():
    """The tool author picks a DEFAULT. The operator's assignment wins, which is the whole
    point of naming sets rather than declaring rules in the decorator."""
    criteria = AutoApprovalCriteria(
        sets={
            "financial": AutoApprovalCriteriaSet(effect="Moves money."),
            "read_only": AutoApprovalCriteriaSet(effect="Reads a record."),
        },
        tools={"issue_refund": "read_only"},
        default="financial",
    )
    assert criteria.set_name_for("issue_refund", declared="financial") == "read_only"
    assert criteria.for_tool("issue_refund", declared="financial").effect == "Reads a record."


def test_a_declared_set_outranks_the_catch_all():
    criteria = AutoApprovalCriteria(
        sets={
            "financial": AutoApprovalCriteriaSet(effect="Moves money."),
            "cautious": AutoApprovalCriteriaSet(effect="Unknown."),
        },
        default="cautious",
    )
    assert criteria.for_tool("issue_refund", declared="financial").effect == "Moves money."


def test_the_catch_all_covers_a_tool_nobody_assigned():
    """A tool with no assignment and no declared set of its own is still judged — against
    the catch-all — so adding a tool does not silently create an ungoverned hole."""
    criteria = AutoApprovalCriteria(
        sets={"cautious": AutoApprovalCriteriaSet(effect="Unknown.")}, default="cautious"
    )
    assert criteria.for_tool("brand_new").effect == "Unknown."


@pytest.mark.parametrize(
    "criteria",
    [
        pytest.param(AutoApprovalCriteria(), id="nothing configured at all"),
        pytest.param(
            AutoApprovalCriteria(tools={"delete_account": "typo"}, default="cautious"),
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
def test_every_way_of_having_no_rules_resolves_to_nothing(criteria):
    """All three mean the same thing to the gate — there is nothing to judge — so the gate
    escalates and never calls an evaluator. The enforcement is asserted against the runner
    in test_agent_workflow_runner.py; this pins the resolution the gate reads."""
    assert criteria.for_tool("delete_account") is None


def test_a_dangling_set_name_is_still_reported_by_name():
    """Escalating is the safe direction either way, but an unregistered name is a typo in
    the operator's configuration, not a deliberate posture — so the name survives resolution
    even though the set does not, and the gate logs it."""
    criteria = AutoApprovalCriteria(tools={"delete_account": "financail"})
    assert criteria.for_tool("delete_account") is None
    assert criteria.set_name_for("delete_account") == "financail"


def test_thresholds_come_from_the_governing_set_then_the_agent_wide_default():
    """Per-set thresholds are the reason sets are named: a refund and a lookup should not
    share one confidence bar."""
    criteria = AutoApprovalCriteria(
        sets={
            "financial": AutoApprovalCriteriaSet(effect="e", min_confidence=0.95),
            "read_only": AutoApprovalCriteriaSet(effect="e"),
        },
        tools={"issue_refund": "financial", "get_order": "read_only"},
        min_confidence=0.7,
        escalate_if_irreversible_above=0.4,
    )
    assert _ctx(tool_name="issue_refund", criteria=criteria).thresholds == (0.95, 0.4)
    # The read_only set set neither, so both fall through to the agent-wide values.
    assert _ctx(tool_name="get_order", criteria=criteria).thresholds == (0.7, 0.4)


# ---------------------------------------------------------------------------
# Composition — thresholds, and every unsure path ending at a human
# ---------------------------------------------------------------------------


def test_a_confident_approve_of_a_reversible_call_is_approved():
    decision = _decide(_answer(verdict="approve", verdict_confidence=0.95, irreversible=0.02))
    assert decision.verdict is AutoApprovalVerdict.APPROVE
    # The reason is the audit trail for a decision no human made, so it names the model
    # that made it and the numbers it made it on.
    assert "jev/jev-1.0" in decision.reason
    assert "95%" in decision.reason


def test_a_confident_deny_is_denied():
    decision = _decide(_answer(verdict="deny", verdict_confidence=0.9))
    assert decision.verdict is AutoApprovalVerdict.DENY
    assert "the approval criteria forbid this call" in decision.reason


def test_an_explicit_escalate_goes_to_a_human():
    decision = _decide(_answer(verdict="escalate", verdict_confidence=0.9))
    assert decision.verdict is AutoApprovalVerdict.ESCALATE


@pytest.mark.parametrize("verdict", ["approve", "deny", "escalate"])
def test_low_confidence_escalates_whatever_the_verdict(verdict):
    """Confidence is checked BEFORE the label is read. A spread-out distribution is not an
    answer, and that includes a refusal — an unsure deny is still an unsure decision."""
    decision = _decide(
        _answer(verdict=verdict, verdict_confidence=0.6), min_confidence=0.8
    )
    assert decision.verdict is AutoApprovalVerdict.ESCALATE
    assert "below the 80% confidence" in decision.reason


def test_an_irreversible_call_is_escalated_even_when_the_policy_allows_it():
    """The one place the harness overrides a confident approve: a mistake that cannot be
    taken back is worth a person's two seconds."""
    decision = _decide(
        _answer(verdict="approve", verdict_confidence=0.99, irreversible=0.8),
        escalate_if_irreversible_above=0.5,
    )
    assert decision.verdict is AutoApprovalVerdict.ESCALATE
    assert "irreversible" in decision.reason


def test_the_irreversibility_ceiling_can_be_turned_off():
    decision = _decide(
        _answer(verdict="approve", verdict_confidence=0.99, irreversible=0.99),
        escalate_if_irreversible_above=None,
    )
    assert decision.verdict is AutoApprovalVerdict.APPROVE


def test_irreversibility_never_rescues_a_deny_or_creates_an_approve():
    """The ceiling gates APPROVE only — it cannot flip a refusal, and a call that looks
    perfectly safe still cannot be auto-approved if the policy said no."""
    assert (
        _decide(_answer(verdict="deny", verdict_confidence=0.95, irreversible=0.0)).verdict
        is AutoApprovalVerdict.DENY
    )
    assert (
        _decide(_answer(verdict="escalate", verdict_confidence=0.95, irreversible=0.0)).verdict
        is AutoApprovalVerdict.ESCALATE
    )


def test_an_unrecognized_verdict_escalates():
    """Unreachable through a Choice, which can only return an offered label — asserted
    anyway because the one acceptable failure mode for an approval gate is asking a
    human too often."""
    decision = _decide(_answer(verdict="maybe", verdict_confidence=0.99))
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

    request = build_request(_ctx())
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
    assert _decide(answer).verdict is AutoApprovalVerdict.DENY


# ---------------------------------------------------------------------------
# End to end — the approver driving a real gate in a real workflow
# ---------------------------------------------------------------------------
#
# The Jev ACTIVITY is stood in for (a live model in a test would be neither cheap nor
# deterministic), but everything around it is real: the workflow-side approver, dispatch of
# the activity BY NAME, the composition, the approval gate, and the published events. What
# this proves is the wiring that the pure tests above cannot — that a value returned from
# jev_evaluator() is a working auto_mode_evaluator.


@agent.activity_tool_defn(auto_approval_criteria="financial")
async def refund_customer(amount: int) -> str:
    """Refund a customer. Moves real money."""
    return f"refunded:{amount}"


# The canary. An ``Injected[...]`` parameter is supplied by the WORKFLOW per call and hidden
# from the model's tool schema — it is where a caller puts an API token, a tenant secret, a
# signed URL. It must never be shown to ANY model, and the auto mode evaluator is a model.
INJECTED_SECRET = "s3cr3t-injected-credential-must-never-reach-a-model"


@agent.tool_defn(auto_approval_criteria="financial")
async def refund_inline_with_credential(amount: int, api_token: Injected[str]) -> str:
    """Refund a customer inline using the caller's payment credential. Moves real money."""
    return f"refunded:{amount}"


@agent.activity_tool_defn(auto_approval_criteria="financial")
async def refund_with_credential(amount: int, api_token: Injected[str]) -> str:
    """Refund a customer using the caller's payment credential. Moves real money."""
    return f"refunded:{amount}"


# The rules the tool's declared set name resolves to. Configuration, so the same tool can be
# judged differently per deployment without touching its code.
_PROBE_CRITERIA = AutoApprovalCriteria(
    sets={
        "financial": AutoApprovalCriteriaSet(
            effect="Moves real money out to a customer. Cannot be taken back.",
            approve_when=("the amount is under 50",),
            deny_when=("the amount is 50 or more",),
        )
    },
    context={"environment": "test"},
)


@workflow.defn
@agent.defn
class JevApprovalProbeAgent:
    """Gates everything, with AUTO MODE on, so every gated call goes to the Jev approver."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.auto_mode(),
            auto_approval_criteria_default=_PROBE_CRITERIA,
            auto_mode_evaluator=jev_evaluator(),
        )

    @agent.accepts
    async def disable_auto_mode(self, message: TextMessage) -> TextReply:
        """Hand decisions back to a human without disturbing the allow-list."""
        self._runner.set_approval_policy(
            self._runner.approval_policy.with_auto_mode(False)
        )
        return TextReply(text="auto mode off")

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

    @agent.accepts
    async def act_with_credential(self, message: TextMessage) -> TextReply:
        """Try one refund through the tool that takes an injected credential."""
        try:
            result = await self._runner.run_tool(
                "r1",
                refund_with_credential,
                int(message.text),
                injections={"api_token": INJECTED_SECRET},
            )
        except agent.ToolApprovalDenied as e:
            result = f"denied:{e.reason}"
        return TextReply(text=result)

    @agent.accepts
    async def act_inline_with_credential(self, message: TextMessage) -> TextReply:
        """Same, through an INLINE tool — the path Code Mode host calls and subagent
        toolsets also take."""
        try:
            result = await self._runner.run_tool(
                "r1",
                refund_inline_with_credential,
                int(message.text),
                injections={"api_token": INJECTED_SECRET},
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
        activities=[
            agent.tool_activity(refund_customer),
            agent.tool_activity(refund_with_credential),
            fake_activity,
        ],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _run_once(
    client,
    task_queue: str,
    amount: str,
    *,
    config: AgentConfig | None = None,
    message_type: str = "act",
) -> list[AgentEvent]:
    handle = await client.start_workflow(
        JevApprovalProbeAgent.run,
        config if config is not None else AgentConfig(),
        id=f"jev-probe-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type=message_type, payload={"text": amount}),
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

    # The question the workflow actually asked, as recorded in the activity's input — which
    # is the whole audit record for a decision no human made. It proves the full resolution
    # chain ran in-workflow: the tool's decorator declared `auto_approval_criteria="financial"`,
    # the runner looked that name up in the live criteria, and the rules behind it are what
    # reached the model.
    assert len(seen) == 1
    state = seen[0].state
    assert state["approval_criteria"] == {
        "what_calls_to_this_tool_do": (
            "Moves real money out to a customer. Cannot be taken back."
        ),
        "approve_when": ["the amount is under 50"],
        "deny_when": ["the amount is 50 or more"],
        "operating_context": {"environment": "test"},
    }
    assert state["tool"]["description"] == "Refund a customer. Moves real money."
    assert state["call_arguments"] == {"amount": 20}

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
        # Which rules produced it, and which generation of them. Criteria can be edited
        # mid-session, so a stored verdict that did not name its rulebook could not be
        # re-read once the rules behind it moved on.
        "criteria_set": "financial",
        "criteria_version": 0,
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


# ---------------------------------------------------------------------------
# End to end — auto mode as a MODE the policy owns
# ---------------------------------------------------------------------------


async def test_end_to_end_auto_mode_off_never_asks_the_model():
    """The posture an operator asked to be possible: their explicit static approvals, and
    their own eyes on everything else. The evaluator is wired and the criteria are
    configured, but the policy says no — so no model is asked, no evaluation bracket is
    published at all, and the call simply waits for a person.

    Driven through ``AgentConfig.approval_policy``, which is how a client imposes its own
    posture over the agent author's default."""
    fake, seen = _fake_jev("approve", confidence=0.99, irreversible=0.0)
    async with _jev_env(fake) as (client, task_queue):
        handle = await client.start_workflow(
            JevApprovalProbeAgent.run,
            AgentConfig(approval_policy=ToolApprovalPolicy.always_require_human_approval()),
            id=f"jev-probe-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        # The turn parks in the human gate, so the update cannot be awaited to completion.
        await handle.start_update(
            SEND_AGENT_MESSAGE_UPDATE,
            AgentMessage(type="act", payload={"text": "20"}),
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            result_type=AgentMessageReply,
        )

        async def gated() -> bool:
            status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
            return any(p.tool_id == "r1" for p in status.pending_approvals)

        async with asyncio.timeout(20):
            while not await gated():
                await asyncio.sleep(0.05)

        # The whole point: nothing was asked of the model, and the call is waiting on a
        # human exactly as it would be with no evaluator wired at all.
        assert seen == []
        status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
        assert status.approval_policy.auto_mode_enabled is False
        # Still advertised, so a client can offer the user the toggle.
        assert status.has_auto_approval_evaluator is True

        events: list[AgentEvent] = []
        stream = WorkflowStreamClient.create(client, handle.id)
        async with asyncio.timeout(20):
            async for item in stream.subscribe(
                topics=[TURN_EVENTS_TOPIC],
                from_offset=0,
                result_type=AgentEvent,
                poll_cooldown=timedelta(milliseconds=10),
            ):
                events.append(item.data)
                if item.data.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED:
                    break

    # Not even a bracket: auto mode being off means nothing is evaluated, so unlike an
    # ESCALATE there is no started/ended pair to explain. The call went from requested
    # straight to waiting on a person.
    assert _tool_event_types(events) == [AgentEventType.TOOL_APPROVAL_REQUESTED]


async def test_end_to_end_no_criteria_never_reaches_the_evaluator():
    """Auto mode ON, evaluator wired, but the caller replaced the agent's criteria with an
    empty rulebook. The HARNESS escalates: no model call, and no evaluation bracket either,
    because nothing was evaluated.

    This is the guarantee the evaluator relies on rather than implements. `jev_evaluator`
    has no "are there rules?" branch at all — it cannot, since `ctx.criteria_set` is
    non-optional — so if the gate did not enforce this, an empty rulebook would go to the
    model, which is the input most likely to come back a confident approve."""
    fake, seen = _fake_jev("approve", confidence=0.99, irreversible=0.0)
    async with _jev_env(fake) as (client, task_queue):
        handle = await client.start_workflow(
            JevApprovalProbeAgent.run,
            AgentConfig(auto_approval_criteria=AutoApprovalCriteria()),
            id=f"jev-probe-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        await handle.start_update(
            SEND_AGENT_MESSAGE_UPDATE,
            AgentMessage(type="act", payload={"text": "20"}),
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            result_type=AgentMessageReply,
        )

        async def gated() -> bool:
            status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
            return any(p.tool_id == "r1" for p in status.pending_approvals)

        async with asyncio.timeout(20):
            while not await gated():
                await asyncio.sleep(0.05)

        assert seen == []

        events: list[AgentEvent] = []
        stream = WorkflowStreamClient.create(client, handle.id)
        async with asyncio.timeout(20):
            async for item in stream.subscribe(
                topics=[TURN_EVENTS_TOPIC],
                from_offset=0,
                result_type=AgentEvent,
                poll_cooldown=timedelta(milliseconds=10),
            ):
                events.append(item.data)
                if item.data.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED:
                    break

    # Requested, then waiting on a person. Nothing was evaluated, so unlike an ESCALATE
    # verdict there is no started/ended pair to explain — exactly as when auto mode is off.
    assert _tool_event_types(events) == [AgentEventType.TOOL_APPROVAL_REQUESTED]


async def test_an_injected_parameter_never_reaches_the_evaluator():
    """SECURITY. An ``Injected[...]`` parameter must not appear anywhere in the state put to
    the auto mode evaluator.

    Injected parameters exist precisely because their values must not be model-visible: the
    WORKFLOW supplies them per call, they are stripped from the tool schema the driving model
    sees, and they are the natural home for an API token or a tenant secret. An auto mode
    evaluator is another model, reached over another network call to another vendor — so
    "hidden from the model" has to mean hidden from it too, not merely from the one choosing
    the call.

    Asserted against the ACTIVITY'S INPUT rather than against the context object, because the
    activity input is the payload that actually leaves the worker: whatever is in it is what a
    third party receives and what lands in workflow history forever. Scanned as serialized
    JSON so the check cannot be fooled by the secret sitting somewhere unexpected in the state
    (nested under the criteria, the tool description, the operating context) instead of the
    argument it came in as."""
    fake, seen = _fake_jev("approve", confidence=0.95, irreversible=0.1)
    async with _jev_env(fake) as (client, task_queue):
        events = await _run_once(
            client, task_queue, "20", message_type="act_with_credential"
        )

    assert len(seen) == 1
    request = seen[0]

    # The model-facing argument is there; the injected one is not, by name or by value.
    assert request.state["call_arguments"] == {"amount": 20}
    assert "api_token" not in request.state["call_arguments"]

    # Nothing anywhere in the request carries it — state, questions, model id and all.
    serialized = request.model_dump_json()
    assert INJECTED_SECRET not in serialized
    assert "api_token" not in serialized

    # And the call really did run with the credential, so this is not passing by accident of
    # the injection never having been supplied.
    assert _reply(events) == "refunded:20"


async def test_an_injected_parameter_never_reaches_the_evaluator_from_an_inline_tool():
    """The same guarantee on the INLINE dispatch path.

    Worth asserting separately: ``tool_defn`` binds its own arguments, and it is the path that
    Code Mode host calls, callback tools and subagent toolsets all funnel through — so a leak
    here would be a leak in most of the harness at once, not just in one decorator."""
    fake, seen = _fake_jev("approve", confidence=0.95, irreversible=0.1)
    async with _jev_env(fake) as (client, task_queue):
        events = await _run_once(
            client, task_queue, "20", message_type="act_inline_with_credential"
        )

    assert len(seen) == 1
    assert seen[0].state["call_arguments"] == {"amount": 20}
    serialized = seen[0].model_dump_json()
    assert INJECTED_SECRET not in serialized
    assert "api_token" not in serialized
    assert _reply(events) == "refunded:20"
