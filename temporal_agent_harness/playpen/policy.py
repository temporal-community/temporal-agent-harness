"""Workflow-side: decide every Code Mode host call against a Dogwood policy.

:class:`DogwoodPolicy` plugs into the two seams a Code Mode agent already has:

* its :attr:`~DogwoodPolicy.evaluator` is the runner's ``auto_mode_evaluator=`` — the harness
  puts every gated call to it, and it answers allow, deny, or escalate to a person;
* its :meth:`~DogwoodPolicy.record_host_call_result` is the ``code_mode_tool``'s
  ``on_host_call_result=`` observer — every host call that completes is added to the trace with
  its result, so a later call can be judged against what earlier calls returned.

The trace lives here, in workflow state, and each decision ships it whole to the
``dogwood_decide`` activity (see :mod:`.activity`). Decisions are serialized: two calls the
script gathered concurrently are decided one after the other, each against a trace that
includes the other's admission, so "never refund the same order twice" holds even for two
refunds issued in the same ``asyncio.gather``.

A call the harness lets through without asking the evaluator — a tool pre-approved by name —
still enters the trace when it completes, as ``admitted`` then ``response``, so a policy never
loses sight of what actually ran.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness.agent_protocol import (
    AutoApprovalContext,
    AutoApprovalCriteria,
    AutoApprovalCriteriaSet,
    AutoApprovalDecision,
    AutoApprovalVerdict,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_workflow import AUTO_MODE_EVALUATOR_ATTR
from temporal_agent_harness.harness.code_mode import HostCallResult

from .models import (
    DOGWOOD_DECIDE_ACTIVITY,
    DogwoodDecideInput,
    DogwoodDecision,
    DogwoodEvent,
)

DEFAULT_ACTIVITY_CONFIG = ActivityConfig(
    start_to_close_timeout=timedelta(seconds=30),
    # A rejected policy or trace is non-retryable at the source; this bounds the transient case.
    retry_policy=RetryPolicy(maximum_attempts=3),
)

_VERDICTS = {
    "allow": AutoApprovalVerdict.APPROVE,
    "deny": AutoApprovalVerdict.DENY,
    "escalate": AutoApprovalVerdict.ESCALATE,
}

# Auto mode consults an evaluator only for a tool some criteria set governs. Dogwood's rules
# live in the policy file, not in criteria, so one catch-all set routes every tool to it.
_CRITERIA_SET = "dogwood"


class DogwoodPolicy:
    """Decide each host call of one agent session against the named Dogwood policy.

    Construct it in the agent's ``@agent.init`` and wire it into the runner and the Code Mode
    tool::

        self._policy = DogwoodPolicy("support")
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=DogwoodPolicy.approval_policy(pre_approved=[CODE_TOOL]),
            auto_approval_criteria_default=DogwoodPolicy.criteria(),
            auto_mode_evaluator=self._policy.evaluator,
        )
        self._code_tool = agent.code_mode_tool(
            TOOLS, name=CODE_TOOL, on_host_call_result=self._policy.record_host_call_result
        )

    ``policy`` names a directory in the worker's :class:`~.activity.DogwoodPolicyStore`.
    """

    def __init__(self, policy: str, *, activity_config: ActivityConfig | None = None) -> None:
        self._policy = policy
        self._activity_config: ActivityConfig = {**(activity_config or DEFAULT_ACTIVITY_CONFIG)}
        self._history: list[DogwoodEvent] = []
        self._admitted: set[str] = set()
        self._lock = asyncio.Lock()
        self._last_timestamp = 0

        # A plain function rather than the bound method, so it can carry the label the harness
        # names this evaluator by on its evaluation events.
        async def evaluate(ctx: AutoApprovalContext) -> AutoApprovalDecision:
            return await self._evaluate(ctx)

        setattr(evaluate, AUTO_MODE_EVALUATOR_ATTR, f"dogwood:{policy}")
        self._evaluator = evaluate

    @staticmethod
    def approval_policy(*, pre_approved: Iterable[str] = ()) -> ToolApprovalPolicy:
        """The runner policy that sends every call to the evaluator, except ``pre_approved``
        tools — typically the run-code tool itself, which does nothing until its script makes a
        host call, and each of those is decided on its own."""
        return ToolApprovalPolicy.auto_mode(pre_approved_tools=pre_approved)

    @staticmethod
    def criteria() -> AutoApprovalCriteria:
        """The criteria that route every tool to the Dogwood evaluator."""
        return AutoApprovalCriteria(
            sets={
                _CRITERIA_SET: AutoApprovalCriteriaSet(
                    effect="Decided by the agent's Dogwood policy, against the session's trace."
                )
            },
            default=_CRITERIA_SET,
        )

    @property
    def evaluator(self) -> Any:
        """The ``auto_mode_evaluator=`` to hand the runner."""
        return self._evaluator

    @property
    def history(self) -> list[DogwoodEvent]:
        """The trace so far, oldest first."""
        return list(self._history)

    async def _evaluate(self, ctx: AutoApprovalContext) -> AutoApprovalDecision:
        async with self._lock:
            request = self._event("request", ctx.tool_name, ctx.tool_id, _json_native(ctx.tool_input))
            decision: DogwoodDecision = await workflow.execute_activity(
                DOGWOOD_DECIDE_ACTIVITY,
                DogwoodDecideInput(
                    policy=self._policy,
                    principal=workflow.info().workflow_id,
                    history=self._history,
                    request=request,
                ),
                result_type=DogwoodDecision,
                **self._activity_config,
            )
            # Every attempt is history, whatever the verdict; only an allowed one is admitted
            # now. An escalated call is admitted if and when it runs, which is when a person
            # has approved it (see record_host_call_result).
            self._history.append(request)
            if decision.verdict == "allow":
                self._admit(ctx.tool_name, ctx.tool_id, request.input)
        return AutoApprovalDecision(
            verdict=_VERDICTS[decision.verdict],
            reason=decision.reason,
            details={
                "policy": self._policy,
                "policy_sha256": decision.policy_sha256,
                "dogwood_verdict": decision.verdict,
                "rules": [r.id for r in decision.rules],
            },
        )

    def record_host_call_result(self, result: HostCallResult) -> None:
        """Add a completed host call to the trace. The ``on_host_call_result=`` observer."""
        if result.tool_id not in self._admitted:
            self._admit(result.tool_name, result.tool_id, result.tool_input)
        self._history.append(
            self._event(
                "response",
                result.tool_name,
                result.tool_id,
                result.tool_input,
                output=_json_native(result.result),
            )
        )

    def _admit(self, action: str, tool_id: str, tool_input: dict[str, Any]) -> None:
        self._admitted.add(tool_id)
        self._history.append(self._event("admitted", action, tool_id, tool_input))

    def _event(
        self,
        kind: str,
        action: str,
        tool_id: str,
        tool_input: dict[str, Any],
        *,
        output: Any = None,
    ) -> DogwoodEvent:
        if output is not None and not isinstance(output, dict):
            raise TypeError(
                f"{action} returned {type(output).__name__}; a Dogwood response needs a record. "
                "Return a model or a dict from the tool."
            )
        # Workflow time, never decreasing, in the whole seconds Dogwood's windows count in.
        self._last_timestamp = max(self._last_timestamp, int(workflow.time()))
        return DogwoodEvent(
            timestamp=self._last_timestamp,
            action=action,
            kind=kind,  # type: ignore[arg-type]
            request_id=tool_id,
            input=tool_input,
            output=output,
        )


def _json_native(value: Any) -> Any:
    """``value`` as JSON-native data, the form a Code Mode script and the trace both see."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_json_native(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_native(v) for k, v in value.items()}
    return value
