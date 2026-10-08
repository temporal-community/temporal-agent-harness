"""Workflow-safe evaluator built on the OpenAI Decisions API."""

from __future__ import annotations

import json
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
    DEFAULT_OPENAI_DECISIONS_MODEL,
    OPENAI_DECISIONS_APPROVAL_ACTIVITY,
    OpenAIDecisionAnswer,
    OpenAIDecisionRequest,
)

DEFAULT_ACTIVITY_CONFIG = ActivityConfig(
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy=RetryPolicy(maximum_attempts=3),
    summary="openai_decisions_approval",
)

ExtraState = Mapping[str, Any] | Callable[[AutoApprovalContext], Mapping[str, Any]]


def openai_decisions_evaluator(
    *,
    model: str = DEFAULT_OPENAI_DECISIONS_MODEL,
    extra_state: ExtraState | None = None,
    activity_config: ActivityConfig | None = None,
) -> Callable[[AutoApprovalContext], Any]:
    """Build an OpenAI Decisions API evaluator for ``auto_mode_evaluator=``.

    Operator rules and thresholds remain in :class:`AutoApprovalCriteria`; this factory
    only selects the model, optional extra state, and activity timeout/retry settings.
    The API's typed Choice and Predicate answers are combined with those thresholds in
    deterministic workflow code.
    """
    config: ActivityConfig = {**(activity_config or DEFAULT_ACTIVITY_CONFIG)}

    async def evaluate(ctx: AutoApprovalContext) -> AutoApprovalDecision:
        request = build_request(
            ctx,
            model=model,
            extra_state=_resolve_extra_state(extra_state, ctx),
        )
        answer: OpenAIDecisionAnswer = await workflow.execute_activity(
            OPENAI_DECISIONS_APPROVAL_ACTIVITY,
            request,
            result_type=OpenAIDecisionAnswer,
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

    setattr(evaluate, AUTO_MODE_EVALUATOR_ATTR, "openai_decisions_evaluator")
    return evaluate


def _resolve_extra_state(
    extra_state: ExtraState | None, ctx: AutoApprovalContext
) -> Mapping[str, Any] | None:
    if extra_state is None:
        return None
    if callable(extra_state):
        return extra_state(ctx)
    return extra_state


def _rules(criteria_set: AutoApprovalCriteriaSet, ctx: AutoApprovalContext) -> dict[str, Any]:
    rules: dict[str, Any] = {}
    if criteria_set.effect:
        rules["what_calls_to_this_tool_do"] = criteria_set.effect
    if criteria_set.approve_when:
        rules["approve_when"] = list(criteria_set.approve_when)
    if criteria_set.deny_when:
        rules["deny_when"] = list(criteria_set.deny_when)
    if criteria_set.escalate_when:
        rules["escalate_when"] = list(criteria_set.escalate_when)
    if ctx.criteria.context:
        rules["operating_context"] = dict(ctx.criteria.context)
    return rules


def build_request(
    ctx: AutoApprovalContext,
    *,
    model: str = DEFAULT_OPENAI_DECISIONS_MODEL,
    extra_state: Mapping[str, Any] | None = None,
) -> OpenAIDecisionRequest:
    """Compose the typed request in workflow code without importing the OpenAI SDK."""
    state: dict[str, Any] = {
        "situation": (
            "An AI agent wants to run a tool. Decide whether the exact call may run "
            "unattended, must be refused, or should wait for a person."
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

    input_text = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return OpenAIDecisionRequest(
        input=input_text,
        model=model,
        questions=[
            {
                "type": "choice",
                "name": "verdict",
                "choices": [
                    {
                        "value": "approve",
                        "description": "Run unattended only when the criteria clearly permit this exact call.",
                    },
                    {
                        "value": "deny",
                        "description": "Refuse the call only when the criteria clearly forbid it.",
                    },
                    {
                        "value": "escalate",
                        "description": "Leave the call for a person if the criteria are silent, ambiguous, or insufficient.",
                    },
                ],
                "instructions": (
                    "Apply approval_criteria as the only authority. Judge the exact tool "
                    "description and call_arguments. deny_when and escalate_when take "
                    "precedence over approve_when. Silence is never permission. Treat "
                    "declared_inherently_safe only as a hint; it cannot override the "
                    "criteria. Choose escalate whenever the criteria do not clearly "
                    "permit or forbid this call."
                ),
            },
            {
                "type": "predicate",
                "name": "irreversible",
                "instructions": (
                    "Could this exact call cause an effect that cannot be undone if it "
                    "turns out to be a mistake? Treat destruction, moving money, or "
                    "sending something outside the system as irreversible; reads and "
                    "computations are reversible."
                ),
            },
        ],
    )


def decide(
    answer: OpenAIDecisionAnswer,
    *,
    min_confidence: float,
    escalate_if_irreversible_above: float | None,
    criteria_set_name: str = "",
    criteria_version: int = 0,
) -> AutoApprovalDecision:
    """Apply operator thresholds to the API's raw typed answers."""
    verdicts = {
        "approve": AutoApprovalVerdict.APPROVE,
        "deny": AutoApprovalVerdict.DENY,
        "escalate": AutoApprovalVerdict.ESCALATE,
    }
    verdict = verdicts.get(answer.verdict)
    stats = (
        f"openai/{answer.model}: verdict={answer.verdict} "
        f"(confidence {answer.verdict_confidence:.0%}), "
        f"irreversible {answer.irreversible:.0%}"
    )
    details = {
        "model": answer.model,
        "verdict": answer.verdict,
        "confidence": answer.verdict_confidence,
        "probabilities": dict(answer.verdict_probabilities),
        "irreversible": answer.irreversible,
        "refused": answer.refused,
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

    def escalate(reason: str) -> AutoApprovalDecision:
        return AutoApprovalDecision(
            verdict=AutoApprovalVerdict.ESCALATE,
            reason=f"{stats} — {reason}",
            details=details,
        )

    if answer.refused:
        return escalate("the Decisions API refused an answer; escalated to a human.")
    if answer.verdict_confidence < min_confidence:
        return escalate(
            f"below the {min_confidence:.0%} confidence needed to decide automatically; "
            "escalated to a human."
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
            "approved on the criteria, but the effect looks irreversible (over the "
            f"{escalate_if_irreversible_above:.0%} ceiling); escalated to a human."
        )
    return AutoApprovalDecision(
        verdict=AutoApprovalVerdict.APPROVE,
        reason=f"{stats} — auto-approved.",
        details=details,
    )
