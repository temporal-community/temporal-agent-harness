"""``cedar_evaluator`` — an auto mode evaluator that asks a Cedar tool policy over Nexus.

The policy service is the ``VerifyToolCalls`` Nexus operation of the temporal-untrusted-workers
tool verifiers (its Cedar verifier, or any handler of the same contract). Each gated call becomes
one tool call named ``<action_prefix>/<tool_name>``, with one ``google.protobuf.Value``
parameter:

  * an ordinary tool call sends its model-facing arguments;
  * a Code Mode script sends :func:`script_parameter` — the
    :class:`~temporal_agent_harness.harness.code_mode.ScriptFacts` that
    :func:`~temporal_agent_harness.harness.code_mode.analyze_script` reads off the script, plus
    a classifier's answers when one is configured. Cedar can't read a script; it can read
    those.

The host calls a script makes still reach the gate one by one, under their own names and with
their concrete arguments, so each is judged as an ordinary call.

Configuration is the endpoint and how calls are named. Which tools may run, with which
arguments, is the policy's business, so one evaluator serves every agent.

Verdicts: allowed approves; a denial that names the policy that forbade it denies; a denial
because no policy permits the call escalates to a human, since nobody wrote a rule about it.
Every failure (the operation failing or timing out, a script that doesn't parse, a classifier
error) raises, which the harness turns into an escalation.

Workflow-safe: it runs in the workflow and calls the operation from there, so the worker needs
no activity for it, and the Nexus endpoint must allow the agent's namespace as a caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue
from pydantic.alias_generators import to_camel
from pydantic_core import to_jsonable_python
from temporalio import workflow

from temporal_agent_harness.harness.agent_protocol import (
    AutoApprovalContext,
    AutoApprovalDecision,
    AutoApprovalVerdict,
)
from temporal_agent_harness.harness.agent_workflow import (
    AUTO_MODE_EVALUATOR_ATTR,
    AutoModeEvaluator,
)
from temporal_agent_harness.harness.code_mode.analysis import (
    ScriptClassifier,
    ScriptFacts,
    analyze_script,
)

with workflow.unsafe.imports_passed_through():
    # Importing registers VerifyToolCallsResponse with protobuf, which the SDK needs to decode
    # the operation's "json/protobuf" result. Without it the decode fails the workflow task
    # repeatedly instead of failing this evaluator.
    from .toolpolicy_pb2 import VerifyToolCallsResponse

DEFAULT_SERVICE = "tool-policy"
DEFAULT_OPERATION = "VerifyToolCalls"
DEFAULT_TIMEOUT = timedelta(seconds=5)

_VALUE_TYPE_URL = "type.googleapis.com/google.protobuf.Value"
# How the Cedar verifier words a denial by an explicit `forbid`; a denial because nothing
# permits the call reads "not permitted by any Cedar policy".
_FORBIDDEN_BY = " denied by Cedar policy "


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class VerifyCaller(_Wire):
    """Who makes the tool calls: the agent's workflow."""

    namespace: str
    subject: str
    task_queue: str
    workflow_id: str


class VerifyToolCall(_Wire):
    """One tool call, ``<prefix>/<tool_name>``, with its parameters as protobuf-JSON ``Any``s."""

    name: str
    parameters: list[dict[str, JsonValue]]


class VerifyToolCallsRequest(_Wire):
    """The ``VerifyToolCallsRequest`` of the tool-policy contract, in its JSON mapping."""

    caller: VerifyCaller
    tool_calls: list[VerifyToolCall]


def cedar_evaluator(
    *,
    endpoint: str,
    service: str = DEFAULT_SERVICE,
    operation: str = DEFAULT_OPERATION,
    action_prefix: str | None = None,
    subject: str | None = None,
    script_classifier: ScriptClassifier | None = None,
    timeout: timedelta = DEFAULT_TIMEOUT,
) -> AutoModeEvaluator:
    """Build an auto mode evaluator that asks the ``VerifyToolCalls`` operation on ``endpoint``.

    Args:
        endpoint: The Nexus endpoint serving the tool policy.
        service: The Nexus service name.
        operation: The Nexus operation name.
        action_prefix: Tool calls are named ``<action_prefix>/<tool_name>``, the action a Cedar
            policy names. Defaults to the agent's task queue, which matches the
            temporal-untrusted-workers proxy's ``<taskQueue>/<activityType>``. Set it when the
            task queue varies by deployment, so the policy doesn't have to.
        subject: The caller's subject, Cedar's ``principal`` (``Worker::"<subject>"``).
            Defaults to the agent's workflow type.
        script_classifier: Asked about each Code Mode script after
            :func:`~temporal_agent_harness.harness.code_mode.analyze_script`; its answers go to
            the policy as ``classification``. Omit to send the script's facts alone.
        timeout: How long to wait for a verdict before escalating.
    """

    async def evaluate(ctx: AutoApprovalContext) -> AutoApprovalDecision:
        info = workflow.info()
        if ctx.code_mode is None:
            # Tool arguments may be pydantic models; the policy sees their JSON.
            parameter: dict[str, JsonValue] = to_jsonable_python(ctx.tool_input)
        else:
            facts = analyze_script(ctx.code_mode.script, ctx.code_mode.host_functions)
            classification = (
                await script_classifier(ctx, facts) if script_classifier is not None else None
            )
            parameter = script_parameter(facts, classification)
        call = VerifyToolCall(
            name=f"{action_prefix or info.task_queue}/{ctx.tool_name}",
            parameters=[{"@type": _VALUE_TYPE_URL, "value": parameter}],
        )
        request = VerifyToolCallsRequest(
            caller=VerifyCaller(
                namespace=info.namespace,
                subject=subject or info.workflow_type,
                task_queue=info.task_queue,
                workflow_id=info.workflow_id,
            ),
            tool_calls=[call],
        )
        response = await workflow.create_nexus_client(
            service=service, endpoint=endpoint
        ).execute_operation(
            operation,
            request.model_dump(mode="json", by_alias=True),
            output_type=VerifyToolCallsResponse,
            schedule_to_close_timeout=timeout,
            # The verifiers can't cancel a verification, and a superseded evaluation has
            # nothing left to wait for.
            cancellation_type=workflow.NexusOperationCancellationType.ABANDON,
            summary=call.name,
        )
        return decide(
            response,
            details={
                "endpoint": endpoint,
                "service": service,
                "action": call.name,
                "parameter": parameter,
                "criteria_set": ctx.criteria_set_name,
                "criteria_version": ctx.criteria_version,
            },
        )

    setattr(evaluate, AUTO_MODE_EVALUATOR_ATTR, "cedar_evaluator")
    return evaluate


def script_parameter(
    facts: ScriptFacts, classification: Mapping[str, int] | None = None
) -> dict[str, JsonValue]:
    """What the policy sees for a Code Mode script: ``arg0.data`` in Cedar.

    ``calls`` repeats the keys of ``uses`` as a list, which Cedar reads as a set, so a policy
    can ask ``["search_flights", ...].containsAll(context.parameters.arg0.data.calls)``.
    ``classification`` is left out when there is none, so a policy that needs it fails its
    ``has`` check.
    """
    calls: list[JsonValue] = [name for name in sorted(facts.uses)]
    uses: dict[str, JsonValue] = {
        name: use.model_dump(mode="json") for name, use in facts.uses.items()
    }
    parameter: dict[str, JsonValue] = {
        "calls": calls,
        "uses": uses,
        "has_while_loop": facts.has_while_loop,
        "dynamic_code": facts.dynamic_code,
    }
    if classification is not None:
        answers: dict[str, JsonValue] = dict(classification)
        parameter["classification"] = answers
    return parameter


def decide(
    response: VerifyToolCallsResponse, *, details: Mapping[str, Any] | None = None
) -> AutoApprovalDecision:
    """Map the policy's answer onto a verdict.

    A denial is DENY only when the reason names the policy that forbade the call. A denial
    because no policy permits it (Cedar's default) is ESCALATE: nobody decided against this
    call, so a person should.
    """
    if response.allowed:
        verdict = AutoApprovalVerdict.APPROVE
        reason = response.reason or "allowed by the tool policy"
    elif _FORBIDDEN_BY in response.reason:
        verdict = AutoApprovalVerdict.DENY
        reason = response.reason
    else:
        verdict = AutoApprovalVerdict.ESCALATE
        reason = response.reason or "no tool policy permits this call"
    return AutoApprovalDecision(
        verdict=verdict,
        reason=reason,
        details={**(details or {}), "allowed": response.allowed, "policy_reason": response.reason},
    )
