"""``jev_evaluator`` — the harness's builtin AI approval gate, backed by Jev.

Auto mode for tool approvals. You write the rules once, in prose; every gated tool call is
put to Jev against those rules and comes back ``approve`` / ``deny`` / ``escalate``::

    self._runner = AgentWorkflowRunner(
        config,
        stream=WorkflowStream(),
        approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
        auto_approval_evaluator=jev_evaluator(
            policy=(
                "Approve read-only lookups, and writes scoped to the requesting user's "
                "own workspace. Deny anything that deletes another customer's data or "
                "spends money. Anything touching production infrastructure goes to a "
                "human, however routine it looks."
            ),
        ),
    )

WHY JEV AND NOT A GENERATIVE MODEL: an approval gate is a *classification* with
consequences, and what it needs back is a decision plus a calibrated number saying how
sure the model is — not an essay to parse. Jev answers a typed Choice over exactly three
outcomes with a probability distribution across them, so "not confident enough to decide"
is a first-class, thresholdable answer rather than something to infer from hedging prose.
It is also one fast call, on the critical path of every gated tool call.

WHAT THE MODEL DECIDES AND WHAT CODE DECIDES: the model answers two narrow questions —
which verdict the operator's rules imply, and whether the call is irreversible. It never
decides *policy*. Turning those raw judgments into an action is :func:`decide`, plain code
with explicit thresholds, so the thresholds can be tuned, reviewed, and re-applied to a
past answer without re-running inference.

SAFE BY CONSTRUCTION, in three ways:

1. **Escalating is the default answer**, not approving. Every path that is not a confident
   approve or a confident deny — low confidence, an unrecognized label, an irreversible
   call, a TypeSafe outage, a worker missing the ``jev`` extra — ends at the human gate.
   The approver never fails open.
2. **It can only decide calls the policy already gates.** It is the layer BELOW
   :class:`ToolApprovalPolicy`, so it is consulted only for calls the operator's
   serializable policy declined to auto-approve. It cannot widen that policy.
3. **It is not reachable by the agent.** The Jev call is dispatched straight as an
   activity, never through ``runner.run_tool`` — so it is not a tool, the model cannot
   call it, cannot see it in its tool list, and cannot influence the question asked about
   its own tool call. (Routing it through ``run_tool`` would also recurse: the approval
   gate would gate the approval.)

Every decision is auditable from workflow history alone: the activity's input is the exact
state and questions Jev was asked, its output the raw answers, and the resulting verdict
plus its numbers are published as the ``tool_approval_resolved`` reason.

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
    AutoApprovalVerdict,
    AutoApprovalDecision,
    AutoApprovalContext,
)
from temporal_agent_harness.harness.agent_workflow import AUTO_APPROVAL_EVALUATOR_ATTR

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

# Below this the verdict's probability mass is too spread out to act on, so the call goes
# to a person. 0.8 is a starting point, not a law: the right threshold depends on the tool
# set and on what a wrong call costs, and the docs are explicit that it should be measured
# on real traffic rather than assumed.
DEFAULT_MIN_CONFIDENCE = 0.8

# An approve is downgraded to an escalate when Jev puts the chance that the call cannot be
# undone above this. A reversible mistake is worth automating; an irreversible one is worth
# a person's two seconds. Pass ``None`` to let confident approvals stand on their own.
DEFAULT_IRREVERSIBLE_CEILING = 0.5


ExtraState = Mapping[str, Any] | Callable[[AutoApprovalContext], Mapping[str, Any]]


def jev_evaluator(
    *,
    policy: str,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    escalate_if_irreversible_above: float | None = DEFAULT_IRREVERSIBLE_CEILING,
    model: str | None = DEFAULT_JEV_MODEL,
    extra_state: ExtraState | None = None,
    activity_config: ActivityConfig | None = None,
) -> Callable[[AutoApprovalContext], Any]:
    """Build a Jev-backed auto-approver to pass as ``auto_approval_evaluator=``.

    Args:
        policy: The operator's auto-mode rules, in plain language — what may run
            unattended, what must never run, and what a person has to see. This is the
            ONLY authority the model is given; it is told in as many words not to
            substitute its own sense of risk. Write it as rules about *kinds of calls*
            ("anything that deletes customer data"), not about individual tools — naming
            specific tools is what :class:`ToolApprovalPolicy`'s allow-list already does,
            more cheaply and without a model in the loop.
        min_confidence: Below this, the verdict goes to a human instead. Applies to deny
            as well as approve: an unsure refusal is still an unsure decision.
        escalate_if_irreversible_above: Downgrade an otherwise-confident APPROVE to an
            escalate when Jev puts the probability that the call is irreversible above
            this. ``None`` disables the check.
        model: TypeSafe model id; ``None`` takes the client's own default.
        extra_state: Additional JSON state to show Jev alongside the call — the acting
            user, the environment, whatever the policy actually turns on. Either a mapping
            or a callable taking the :class:`AutoApprovalContext`. A callable runs
            IN-WORKFLOW on every gated call, so it must be deterministic and must not do
            I/O; read from the agent's own state, not from the outside world.
        activity_config: Timeout/retry overrides for the Jev call. Defaults to
            :data:`DEFAULT_ACTIVITY_CONFIG`.

    Returns:
        An async callable of the :data:`AutoApprovalEvaluator` shape.
    """
    config: ActivityConfig = {**(activity_config or DEFAULT_ACTIVITY_CONFIG)}

    async def approve(ctx: AutoApprovalContext) -> AutoApprovalDecision:
        request = build_request(
            ctx, policy=policy, model=model, extra_state=_resolve_extra_state(extra_state, ctx)
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
        return decide(
            answer,
            min_confidence=min_confidence,
            escalate_if_irreversible_above=escalate_if_irreversible_above,
        )

    # Names this evaluator on all three of its evaluation events. Stamped on the CALLABLE
    # because two of the three — the started event and the error event — are published
    # without a decision to read a label from.
    setattr(approve, AUTO_APPROVAL_EVALUATOR_ATTR, "jev_evaluator")
    return approve


def _resolve_extra_state(
    extra_state: ExtraState | None, ctx: AutoApprovalContext
) -> Mapping[str, Any] | None:
    if extra_state is None:
        return None
    if callable(extra_state):
        return extra_state(ctx)
    return extra_state


def build_request(
    ctx: AutoApprovalContext,
    *,
    policy: str,
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
        "approval_policy": policy,
        "tool": {
            "name": ctx.tool_name,
            "description": ctx.tool_description,
            "declared_inherently_safe": ctx.inherently_safe,
        },
        "call_arguments": ctx.tool_input,
    }
    if extra_state:
        state["context"] = dict(extra_state)

    return JevApprovalRequest(
        state=state,
        model=model,
        questions={
            "verdict": {
                "type": "choice",
                "instructions": {
                    "question": (
                        "Under the operator's rules in `approval_policy`, and those rules "
                        "only, what should happen to the call described by `tool` and "
                        "`call_arguments`?"
                    ),
                    "priorities": [
                        "Judge the call as it would actually run: `tool.description` says "
                        "what the tool does, and `call_arguments` are the exact arguments "
                        "it would run with.",
                        "`approval_policy` is the operator's decision and the only "
                        "authority here. Do not substitute your own sense of what is "
                        "risky for what it says — neither to approve something it "
                        "forbids nor to refuse something it allows.",
                        "`tool.declared_inherently_safe` is the tool author's own claim "
                        "that the tool is never unsafe under any input. Treat it as a "
                        "hint; it never overrides `approval_policy`.",
                        "If the rules do not settle this call, choose `escalate`. A person "
                        "is available and asking costs almost nothing; guessing does not.",
                    ],
                },
                "criteria": {
                    "approve": {
                        "what": "Run the call now, unattended, with no human review.",
                        "when": (
                            "`approval_policy` permits calls of this kind, and nothing in "
                            "`call_arguments` crosses a line the policy draws."
                        ),
                        "not_for": (
                            "A call the policy simply does not address — silence is not "
                            "permission; that is `escalate`."
                        ),
                    },
                    "deny": {
                        "what": (
                            "Refuse the call. It never runs, and the agent is told it was "
                            "refused so it can try a different approach."
                        ),
                        "when": (
                            "`approval_policy` rules out calls of this kind, or "
                            "`call_arguments` show this particular call doing something "
                            "the policy forbids."
                        ),
                        "not_for": (
                            "A call that merely looks consequential but that the policy "
                            "does not forbid; that is `escalate`."
                        ),
                    },
                    "escalate": {
                        "what": (
                            "Hold the call and put it in front of a person, who approves "
                            "or denies it themselves."
                        ),
                        "when": (
                            "`approval_policy` does not settle this call: it is silent on "
                            "this kind of call, it is ambiguous, or `call_arguments` are "
                            "not specific enough to tell which side of the policy the "
                            "call falls on."
                        ),
                        "not_for": (
                            "A call the policy plainly permits or plainly forbids — "
                            "deciding those is the point of having the policy."
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
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    escalate_if_irreversible_above: float | None = DEFAULT_IRREVERSIBLE_CEILING,
) -> AutoApprovalDecision:
    """Compose Jev's raw judgments into an action.

    Pure: no Temporal, no network, no clock. This is where policy lives, deliberately
    apart from inference, so the thresholds are reviewable and can be replayed against a
    stored answer.

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
    # judgments AND the thresholds applied to them, which is what makes a past decision
    # re-readable against different thresholds without asking the model again.
    details = _details(
        answer,
        min_confidence=min_confidence,
        escalate_if_irreversible_above=escalate_if_irreversible_above,
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
        return escalate("the approval policy does not settle this call.")
    if verdict is AutoApprovalVerdict.DENY:
        return AutoApprovalDecision(
            verdict=AutoApprovalVerdict.DENY,
            reason=f"{stats} — the approval policy forbids this call.",
            details=details,
        )
    if (
        escalate_if_irreversible_above is not None
        and answer.irreversible > escalate_if_irreversible_above
    ):
        return escalate(
            f"approved on policy, but the effect looks irreversible (over the "
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
) -> dict[str, Any]:
    """The reviewable record of one Jev evaluation: what the model said, and the policy
    that was applied to it.

    Both halves are needed. The judgments alone cannot explain an outcome (a 0.7 confidence
    escalates under one threshold and approves under another), and the thresholds alone say
    nothing about the call. Together they are enough to re-derive the verdict — which is
    exactly what :func:`decide` does, and what a reviewer can re-do by hand from a stored
    event.
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
    }
