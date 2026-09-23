"""``jev_evaluator`` — the harness's builtin auto mode evaluator, backed by Jev.

The MECHANISM half of auto mode. The RULEBOOK is
:class:`~temporal_agent_harness.harness.agent_protocol.AutoApprovalCriteria`, which reaches
this evaluator on the context rather than being closed over here — so the operator's rules
outlive a change of evaluator, and this one holds no configuration of its own beyond which
model to ask::

    self._runner = AgentWorkflowRunner(
        config,
        stream=WorkflowStream(),
        # THE SWITCH: auto mode is a policy mode, not something an evaluator turns on.
        approval_policy_default=ToolApprovalPolicy.auto_mode(
            pre_approved_tools=["get_order"],   # approved above auto mode; never asked
        ),
        # THE RULES: named sets, and which set judges which tool.
        auto_approval_criteria_default=AutoApprovalCriteria(
            sets={
                "read_only": AutoApprovalCriteriaSet(
                    effect="Reads and returns records. Changes nothing.",
                    approve_when=("the record is inside the requesting user's workspace",),
                    min_confidence=0.7,
                ),
                "financial": AutoApprovalCriteriaSet(
                    effect="Moves money. Cannot be taken back once the processor accepts.",
                    deny_when=("the destination is not the account that paid",),
                    min_confidence=0.95,
                ),
            },
            tools={"list_orders": "read_only"},   # covers tools with no decorator, e.g. MCP
            default="read_only",                  # the catch-all
        ),
        # THE EVALUATOR.
        auto_mode_evaluator=jev_evaluator(),
    )

and each tool names its own default set, which the two above outrank::

    @agent.activity_tool_defn(auto_approval_criteria="financial")
    async def issue_refund(order_id: str, amount_cents: int) -> Receipt: ...

WHY JEV AND NOT A GENERATIVE MODEL: an approval gate is a *classification* with
consequences, and what it needs back is a decision plus a calibrated number saying how
sure the model is — not an essay to parse. Jev answers a typed Choice over exactly three
outcomes with a probability distribution across them, so "not confident enough to decide"
is a first-class, thresholdable answer rather than something to infer from hedging prose.
It is also one fast call, on the critical path of every gated tool call.

WHAT THE MODEL DECIDES AND WHAT CODE DECIDES: the model answers two narrow questions —
which verdict the operator's criteria imply, and whether the call is irreversible. It never
decides *policy*. Turning those raw judgments into an action is :func:`decide`, plain code
with explicit thresholds read off the criteria, so an operator can retune them — or an
auditor can re-apply them to a stored answer — without re-running inference.

SAFE BY CONSTRUCTION, in four ways:

1. **Escalating is the default answer**, not approving. Every path that is not a confident
   approve or a confident deny — low confidence, an unrecognized label, an irreversible
   call, a TypeSafe outage, a worker missing the ``jev`` extra — ends at the human gate.
   The approver never fails open.
2. **It is only reached when the operator switched auto mode on.** The gate consults it
   only for calls whose live :class:`ToolApprovalPolicy` has ``auto_mode_enabled``; it
   is a mode the operator owns, not a fallback that activates because an evaluator exists.
3. **It can only decide calls the policy already gated.** It sits BELOW the policy's
   allow-list layers, so it never sees a call they approved and cannot widen them. An
   explicit human "always allow this tool" (``remember=True``) outranks it permanently.
4. **It is not reachable by the agent.** The Jev call is dispatched straight as an
   activity, never through ``runner.run_tool`` — so it is not a tool, the model cannot
   call it, cannot see it in its tool list, and cannot influence the question asked about
   its own tool call. (Routing it through ``run_tool`` would also recurse: the approval
   gate would gate the approval.)

AND IT NEVER RUNS WHEN THERE IS NOTHING TO JUDGE. If no criteria set resolves for the tool —
nobody assigned one, the assigned name is not registered, or the set is empty — the HARNESS
escalates the call to the human gate and never calls this evaluator at all. That is enforced
in the gate, not here (see ``AgentWorkflowRunner._auto_mode_context``): it is a safety
property of auto mode, so it must not depend on an evaluator implementation remembering to
check. Hence ``ctx.criteria_set`` is non-optional, and an unconfigured auto mode costs zero
and changes nothing about which calls reach a person.

Every decision is auditable from workflow history alone: the activity's input is the exact
state and questions Jev was asked (criteria included, resolved), its output the raw answers,
and the resulting verdict plus its numbers, the set that governed it, and the criteria
generation are published as the ``tool_approval_resolved`` reason and details.

This module is WORKFLOW-SAFE. It imports no TypeSafe SDK — it builds plain dicts and
dispatches the activity by name — so the optional ``jev`` extra is needed only on the
WORKER that runs :mod:`.activity`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness.agent_protocol import (
    AutoApprovalContext,
    AutoApprovalCriteriaSet,
    AutoApprovalDecision,
    AutoApprovalVerdict,
)
from temporal_agent_harness.harness.agent_workflow import AUTO_MODE_EVALUATOR_ATTR

from .models import (
    DEFAULT_JEV_MODEL,
    JEV_TOOL_APPROVAL_ACTIVITY,
    JevApprovalAnswer,
    JevApprovalRequest,
)

# The Choice labels Jev picks between. They are the wire form of ``AutoApprovalVerdict`` and
# are mapped back onto it by :func:`decide`; an answer outside this set is treated as an
# escalate rather than trusted.
_VERDICT_LABELS: dict[str, AutoApprovalVerdict] = {
    "approve": AutoApprovalVerdict.APPROVE,
    "deny": AutoApprovalVerdict.DENY,
    "escalate": AutoApprovalVerdict.ESCALATE,
}

# One Jev call sits on the critical path of every gated tool call, so the gate is kept
# short and the retries few: an approval that cannot be reached quickly should become a
# question for a human, not a turn that stalls behind a retry loop.
DEFAULT_ACTIVITY_CONFIG = ActivityConfig(
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy=RetryPolicy(maximum_attempts=3),
    summary="jev_approval",
)


ExtraState = Mapping[str, Any] | Callable[[AutoApprovalContext], Mapping[str, Any]]


def jev_evaluator(
    *,
    model: str | None = DEFAULT_JEV_MODEL,
    extra_state: ExtraState | None = None,
    activity_config: ActivityConfig | None = None,
) -> Callable[[AutoApprovalContext], Any]:
    """Build a Jev-backed auto mode evaluator to pass as ``auto_mode_evaluator=``.

    Takes no rules and no thresholds ON PURPOSE. Those are the operator's, they change over
    a session's life, and they are configuration rather than code — so they live in
    :class:`AutoApprovalCriteria` and arrive on every
    :class:`AutoApprovalContext`. What is left here is the genuinely
    mechanism-specific: which model to ask, what extra live state to show it, and how long
    to wait.

    Args:
        model: TypeSafe model id; ``None`` takes the client's own default.
        extra_state: Additional JSON state to show Jev alongside the call, on top of the
            criteria's own serializable ``context``. Either a mapping or a callable taking
            the :class:`AutoApprovalContext`. Use the callable form for facts that must be
            read from LIVE agent state per call — it runs IN-WORKFLOW on every gated call,
            so it must be deterministic and must not do I/O; read from the agent's own
            state, never from the outside world.
        activity_config: Timeout/retry overrides for the Jev call. Defaults to
            :data:`DEFAULT_ACTIVITY_CONFIG`.

    Returns:
        An async callable of the :data:`AutoModeEvaluator` shape.
    """
    config: ActivityConfig = {**(activity_config or DEFAULT_ACTIVITY_CONFIG)}

    async def approve(ctx: AutoApprovalContext) -> AutoApprovalDecision:
        # No "are there any rules?" check, deliberately. The harness guarantees there are —
        # it resolves the governing set before deciding to call an evaluator at all, and
        # escalates the call itself when none does (see
        # ``AgentWorkflowRunner._auto_mode_context``). That guarantee is why
        # ``ctx.criteria_set`` is non-optional, and why this function has no way to spend a
        # model call on an empty rulebook.
        request = build_request(
            ctx, model=model, extra_state=_resolve_extra_state(extra_state, ctx)
        )
        # Deliberately NOT wrapped in try/except. A TypeSafe outage is an evaluation that
        # FAILED, not one that abstained, and the harness already draws that distinction on
        # the stream: it catches the exception, publishes ``auto_approval_evaluation_error``
        # with the failure, and substitutes an escalate. Catching it here would flatten a
        # failure into a normal verdict and lose that.
        answer: JevApprovalAnswer = await workflow.execute_activity(
            JEV_TOOL_APPROVAL_ACTIVITY,
            request,
            result_type=JevApprovalAnswer,
            **config,
        )
        min_confidence, irreversible_ceiling = ctx.thresholds
        return decide(
            answer,
            min_confidence=min_confidence,
            escalate_if_irreversible_above=irreversible_ceiling,
            criteria_set_name=ctx.criteria_set_name,
            criteria_version=ctx.criteria_version,
        )

    # Names this evaluator on all three of its evaluation events. Stamped on the CALLABLE
    # because two of the three — the started event and the error event — are published
    # without a decision to read a label from.
    setattr(approve, AUTO_MODE_EVALUATOR_ATTR, "jev_evaluator")
    return approve


def _resolve_extra_state(
    extra_state: ExtraState | None, ctx: AutoApprovalContext
) -> Mapping[str, Any] | None:
    if extra_state is None:
        return None
    if callable(extra_state):
        return extra_state(ctx)
    return extra_state


def _rules(criteria_set: AutoApprovalCriteriaSet, ctx: AutoApprovalContext) -> dict[str, Any]:
    """The operator's rules as JSON, flattened to exactly what the model must weigh.

    Empty rule lists are omitted rather than sent as ``[]``: an absent key reads as "the
    operator said nothing about this", which is the truth and is what should push a call
    toward ``escalate``, whereas an empty list invites a model to read it as "nothing is
    forbidden".
    """
    out: dict[str, Any] = {}
    if criteria_set.effect:
        out["what_calls_to_this_tool_do"] = criteria_set.effect
    if criteria_set.approve_when:
        out["approve_when"] = list(criteria_set.approve_when)
    if criteria_set.deny_when:
        out["deny_when"] = list(criteria_set.deny_when)
    if criteria_set.escalate_when:
        out["escalate_when"] = list(criteria_set.escalate_when)
    if ctx.criteria.context:
        out["operating_context"] = dict(ctx.criteria.context)
    return out


def build_request(
    ctx: AutoApprovalContext,
    *,
    model: str | None = DEFAULT_JEV_MODEL,
    extra_state: Mapping[str, Any] | None = None,
) -> JevApprovalRequest:
    """The exact System One request asked about one gated call.

    Pure, and public, so the prompt is testable and inspectable without a worker, a
    workflow, or a TypeSafe key — and so an agent that wants a different question can build
    on this one rather than starting over.

    Both questions are asked in a single request: they are independent judgments over the
    same state, so TypeSafe answers them in parallel and the second costs no extra
    round-trip.
    """
    state: dict[str, Any] = {
        "situation": (
            "An AI agent wants to run a tool. You are the approval gate that decides "
            "whether the call may run unattended, must be refused, or has to be shown to "
            "a person first. The agent chose this tool and these arguments itself."
        ),
        "approval_criteria": _rules(ctx.criteria_set, ctx),
        "tool": {
            "name": ctx.tool_name,
            "description": ctx.tool_description,
            "declared_inherently_safe": ctx.inherently_safe,
        },
        "call_arguments": ctx.tool_input,
    }
    if extra_state:
        state["agent_state"] = dict(extra_state)

    return JevApprovalRequest(
        state=state,
        model=model,
        questions={
            "verdict": {
                "type": "choice",
                "instructions": {
                    "question": (
                        "Under the operator's rules in `approval_criteria`, and those "
                        "rules only, what should happen to the call described by `tool` "
                        "and `call_arguments`?"
                    ),
                    "priorities": [
                        "Judge the call as it would actually run: `tool.description` says "
                        "what the tool does, and `call_arguments` are the exact arguments "
                        "it would run with.",
                        "`approval_criteria` is the operator's decision and the only "
                        "authority here. Do not substitute your own sense of what is "
                        "risky for what it says — neither to approve something it "
                        "forbids nor to refuse something it allows.",
                        "`approval_criteria.deny_when` and `.escalate_when` are binding. "
                        "`.approve_when` describes what a routine call looks like; it "
                        "permits nothing that a deny or escalate rule also matches, so "
                        "when rules conflict the stricter one wins.",
                        "An absent key in `approval_criteria` means the operator said "
                        "nothing about it. Silence is never permission.",
                        "`tool.declared_inherently_safe` is the tool author's own claim "
                        "that the tool is never unsafe under any input. Treat it as a "
                        "hint; it never overrides `approval_criteria`.",
                        "If the rules do not settle this call, choose `escalate`. A person "
                        "is available and asking costs almost nothing; guessing does not.",
                    ],
                },
                "criteria": {
                    "approve": {
                        "what": "Run the call now, unattended, with no human review.",
                        "when": (
                            "`approval_criteria` permits calls of this kind, and nothing "
                            "in `call_arguments` crosses a line the criteria draw."
                        ),
                        "not_for": (
                            "A call the criteria simply do not address — silence is not "
                            "permission; that is `escalate`."
                        ),
                    },
                    "deny": {
                        "what": (
                            "Refuse the call. It never runs, and the agent is told it was "
                            "refused so it can try a different approach."
                        ),
                        "when": (
                            "`approval_criteria` rule out calls of this kind, or "
                            "`call_arguments` show this particular call doing something "
                            "the criteria forbid."
                        ),
                        "not_for": (
                            "A call that merely looks consequential but that the criteria "
                            "do not forbid; that is `escalate`."
                        ),
                    },
                    "escalate": {
                        "what": (
                            "Hold the call and put it in front of a person, who approves "
                            "or denies it themselves."
                        ),
                        "when": (
                            "`approval_criteria` do not settle this call: they are silent "
                            "on this kind of call, they are ambiguous, or "
                            "`call_arguments` are not specific enough to tell which side "
                            "of the criteria the call falls on."
                        ),
                        "not_for": (
                            "A call the criteria plainly permit or plainly forbid — "
                            "deciding those is the point of having them."
                        ),
                    },
                },
            },
            "irreversible": {
                "type": "noul",
                "instructions": (
                    "Independently of whether the call should be allowed: if this call "
                    "runs and turns out to have been a mistake, is its effect one that "
                    "cannot be taken back?"
                ),
                "criteria": {
                    "true": (
                        "Running it causes something that cannot be undone from inside "
                        "this system afterwards — data or a resource destroyed, money "
                        "moved, a message or request sent to someone outside it."
                    ),
                    "false": (
                        "It only reads or computes, or whatever it changes can be changed "
                        "back by a later call."
                    ),
                },
            },
        },
    )


def decide(
    answer: JevApprovalAnswer,
    *,
    min_confidence: float,
    escalate_if_irreversible_above: float | None,
    criteria_set_name: str = "",
    criteria_version: int = 0,
) -> AutoApprovalDecision:
    """Compose Jev's raw judgments into an action.

    Pure: no Temporal, no network, no clock. This is where the operator's numbers are
    APPLIED, deliberately apart from inference, so a stored answer can be replayed against
    different thresholds without asking the model again. The thresholds are passed in
    rather than defaulted here because they belong to the criteria set that governed the
    call — a refund and a lookup do not share one confidence bar.

    The order of the checks is the safety argument:

    1. **Confidence first**, before the verdict is even read. A verdict whose probability
       mass is spread across the three outcomes is not an answer, whichever label happened
       to come out on top — including ``deny``, because an unsure refusal is still an
       unsure decision and a person should make it.
    2. **An unrecognized label escalates.** ``choice`` can only return an offered label, so
       this should be unreachable; it is here because the one acceptable failure mode for
       an approval gate is asking a human too often.
    3. **Irreversibility gates only APPROVE.** It cannot turn an escalate into an approve,
       and it has no bearing on a deny — it exists solely to withhold automation from the
       calls that cannot be taken back.
    """
    verdict = _VERDICT_LABELS.get(answer.verdict)
    stats = (
        f"jev/{answer.model}: verdict={answer.verdict} "
        f"(confidence {answer.verdict_confidence:.0%}), "
        f"irreversible {answer.irreversible:.0%}"
    )
    # Published on ``auto_approval_evaluation_ended`` for every verdict — including an
    # escalate, which resolves nothing and so has no other trace. It carries the raw
    # judgments AND the thresholds applied to them AND which generation of which criteria
    # set produced them, which is what makes a past decision re-readable once the rules
    # behind it have moved on.
    details = _details(
        answer,
        min_confidence=min_confidence,
        escalate_if_irreversible_above=escalate_if_irreversible_above,
        criteria_set_name=criteria_set_name,
        criteria_version=criteria_version,
    )

    def escalate(why: str) -> AutoApprovalDecision:
        return AutoApprovalDecision(
            verdict=AutoApprovalVerdict.ESCALATE, reason=f"{stats} — {why}", details=details
        )

    if answer.verdict_confidence < min_confidence:
        return escalate(
            f"below the {min_confidence:.0%} confidence needed to decide automatically; "
            f"escalated to a human."
        )
    if verdict is None:
        return escalate("unrecognized verdict; escalated to a human.")
    if verdict is AutoApprovalVerdict.ESCALATE:
        return escalate("the approval criteria do not settle this call.")
    if verdict is AutoApprovalVerdict.DENY:
        return AutoApprovalDecision(
            verdict=AutoApprovalVerdict.DENY,
            reason=f"{stats} — the approval criteria forbid this call.",
            details=details,
        )
    if (
        escalate_if_irreversible_above is not None
        and answer.irreversible > escalate_if_irreversible_above
    ):
        return escalate(
            f"approved on the criteria, but the effect looks irreversible (over the "
            f"{escalate_if_irreversible_above:.0%} ceiling); escalated to a human."
        )
    return AutoApprovalDecision(
        verdict=AutoApprovalVerdict.APPROVE,
        reason=f"{stats} — auto-approved.",
        details=details,
    )


def _details(
    answer: JevApprovalAnswer,
    *,
    min_confidence: float,
    escalate_if_irreversible_above: float | None,
    criteria_set_name: str,
    criteria_version: int,
) -> dict[str, Any]:
    """The reviewable record of one Jev evaluation: what the model said, the numbers
    applied to it, and which rules those came from.

    All three are needed. The judgments alone cannot explain an outcome (a 0.7 confidence
    escalates under one threshold and approves under another); the thresholds alone say
    nothing about the call; and neither says anything once the criteria have been edited,
    which they now can be mid-session — hence the set name and generation. Together they
    are enough to re-derive the verdict, which is exactly what :func:`decide` does and what
    a reviewer can re-do by hand from a stored event.
    """
    return {
        "model": answer.model,
        "request_id": answer.request_id,
        "verdict": answer.verdict,
        "confidence": answer.verdict_confidence,
        "probabilities": dict(answer.verdict_probabilities),
        "irreversible": answer.irreversible,
        "usage": {
            "input_tokens": answer.input_tokens,
            "output_tokens": answer.output_tokens,
        },
        "thresholds": {
            "min_confidence": min_confidence,
            "escalate_if_irreversible_above": escalate_if_irreversible_above,
        },
        "criteria_set": criteria_set_name,
        "criteria_version": criteria_version,
    }
