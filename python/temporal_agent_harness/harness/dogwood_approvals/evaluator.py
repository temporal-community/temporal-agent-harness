"""``dogwood_evaluator`` — an auto mode evaluator that asks a Dogwood tool policy over Nexus.

The policy service is the ``tool-policy`` Nexus service of the temporal-untrusted-workers
Dogwood verifier (or any handler of the same contract). Dogwood is Cedar plus temporal
conditions over a history of events, so a policy can judge a call by what came before it in the
same run.

Before asking, the evaluator records what the run has received since it last asked
(``RecordInputEvents``), as history:

  * each message the agent accepted, as an update named after its handler (Dogwood's
    ``update/<handler>``);
  * each tool result, as ``<action_prefix>/<tool_name>/result``, with the result as ``arg0``
    and the call's arguments as ``arg1``.

Then each gated call becomes one ``VerifyToolCalls`` request. The call itself is a tool call
named ``<action_prefix>/<tool_name>`` with one ``google.protobuf.Value`` parameter, Dogwood's
``context.input.arg0``: its model-facing arguments, unless a :data:`PolicyCallBuilder` is
registered for the tool. A builder turns arguments a policy can't read (a SQL query, say) into
facts it can, and may add more checks to the same request (one per table the query reads, say).
The verifier decides a request's calls together: any denial outweighs any escalation, which
outweighs approval.

Configuration is the endpoint and how calls are named. Which tools may run, with which
arguments, is the policy's business, so one evaluator serves every agent.

Verdicts follow the verifier's decision: approved approves, denied denies, escalate goes to a
human. Every failure (the operation failing or timing out, a verifier that can't decide right
now or answers no decision, a builder that raises) raises, which the harness turns into an
escalation.

Workflow-safe: it runs in the workflow and calls the operations from there, so the worker needs
no activity for it, and the Nexus endpoint must allow the agent's namespace as a caller.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel
from pydantic_core import to_jsonable_python
from temporalio import workflow

from temporal_agent_harness.harness.agent_protocol import (
    AutoApprovalContext,
    AutoApprovalDecision,
    AutoApprovalVerdict,
    ReceivedInput,
    ReceivedInputKind,
)
from temporal_agent_harness.harness.agent_workflow import (
    AUTO_MODE_EVALUATOR_ATTR,
    AutoModeEvaluator,
)

with workflow.unsafe.imports_passed_through():
    # Importing registers the response messages with protobuf, which the SDK needs to decode
    # the operations' "json/protobuf" results. Without them the decode fails the workflow task
    # repeatedly instead of failing this evaluator.
    from temporal_agent_harness.harness.dogwood_approvals.toolpolicy_pb2 import (
        DECISION_APPROVED,
        DECISION_DENIED,
        DECISION_ESCALATE,
        Decision,
        RecordInputEventsResponse,
        VerifyToolCallsResponse,
    )

DEFAULT_SERVICE = "tool-policy"
DEFAULT_VERIFY_OPERATION = "VerifyToolCalls"
DEFAULT_RECORD_OPERATION = "RecordInputEvents"
DEFAULT_TIMEOUT = timedelta(seconds=5)

_VALUE_TYPE_URL = "type.googleapis.com/google.protobuf.Value"


class PolicyCall(BaseModel, frozen=True):
    """One more check sent with a gated call: the action ``<action_prefix>/<tool>``, with
    ``arg0`` as its ``context.input.arg0``.

    ``tool`` needn't be a real tool. It names an action the policy has rules for, such as
    ``read_table`` for each table a query reads.
    """

    tool: str
    arg0: dict[str, JsonValue]


class PolicyCalls(BaseModel, frozen=True):
    """What a :data:`PolicyCallBuilder` makes of a gated call: the call's own ``arg0``, the
    checks to send with it, and an ``explanation`` the agent reads if the policy denies it.

    A policy's reason is fixed text; the explanation carries what only this call knows, such as
    why a query couldn't be analyzed, so the agent can correct the call. The policy never sees
    it, and it changes no decision.
    """

    arg0: dict[str, JsonValue]
    checks: list[PolicyCall] = Field(default_factory=list)
    explanation: str | None = None


PolicyCallBuilder = Callable[[AutoApprovalContext], Awaitable[PolicyCalls]]
"""Builds what the policy sees for one tool's calls. It runs in the workflow, so it may run an
activity (parsing a query, say), and anything it raises escalates the call."""


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class VerifyCaller(_Wire):
    """Who makes the tool calls: the agent's workflow."""

    namespace: str
    subject: str
    task_queue: str
    workflow_id: str
    run_id: str


class VerifyToolCall(_Wire):
    """One tool call, ``<prefix>/<tool_name>``, with its parameters as protobuf-JSON ``Any``s."""

    name: str
    parameters: list[dict[str, JsonValue]]


class VerifyToolCallsRequest(_Wire):
    """The ``VerifyToolCallsRequest`` of the tool-policy contract, in its JSON mapping."""

    caller: VerifyCaller
    tool_calls: list[VerifyToolCall]


class InputEventKind(StrEnum):
    """The ``InputEventKind`` values the evaluator records."""

    ACTIVITY_RESULT = "INPUT_EVENT_KIND_ACTIVITY_RESULT"
    UPDATE = "INPUT_EVENT_KIND_UPDATE"


class InputEvent(_Wire):
    """One thing the run received, as ``RecordInputEvents`` takes it.

    The verifier deduplicates an event by its update ID, whatever its kind. A tool result has
    no history event ID of its own to offer, so it carries ``tool-result/<tool call ID>`` there.
    """

    kind: InputEventKind
    name: str
    payloads: list[dict[str, JsonValue]]
    update_id: str


class RecordInputEventsRequest(_Wire):
    """The ``RecordInputEventsRequest`` of the tool-policy contract, in its JSON mapping."""

    caller: VerifyCaller
    events: list[InputEvent]
    from_history_start: bool


def dogwood_evaluator(
    *,
    endpoint: str,
    service: str = DEFAULT_SERVICE,
    verify_operation: str = DEFAULT_VERIFY_OPERATION,
    record_operation: str | None = DEFAULT_RECORD_OPERATION,
    action_prefix: str | None = None,
    subject: str | None = None,
    policy_calls: Mapping[str, PolicyCallBuilder] | None = None,
    timeout: timedelta = DEFAULT_TIMEOUT,
) -> AutoModeEvaluator:
    """Build an auto mode evaluator that asks the tool-policy service on ``endpoint``.

    Args:
        endpoint: The Nexus endpoint serving the tool policy.
        service: The Nexus service name.
        verify_operation: The operation that decides a tool call.
        record_operation: The operation that receives the run's input events, or ``None`` to
            record nothing, for a verifier without it.
        action_prefix: Tool calls are named ``<action_prefix>/<tool_name>``, the action a
            policy names, and tool results ``<action_prefix>/<tool_name>/result``. Defaults to
            the agent's task queue, which matches the temporal-untrusted-workers proxy's
            ``<taskQueue>/<activityType>``. Set it when the task queue varies by deployment,
            so the policy doesn't have to.
        subject: The caller's subject, the policy's ``principal``
            (``Temporal::Worker::"<subject>"``). Defaults to the agent's workflow type.
        policy_calls: Builders keyed by tool name, for tools whose arguments the policy should
            see as something else. A tool without one sends its model-facing arguments.
        timeout: How long to wait for each operation before escalating.
    """
    builders = dict(policy_calls or {})
    # How many of each run's received inputs the verifier has been given. Keyed by run, so an
    # evaluator shared by several runs keeps them apart.
    recorded: dict[str, int] = {}

    async def evaluate(ctx: AutoApprovalContext) -> AutoApprovalDecision:
        info = workflow.info()
        prefix = action_prefix or info.task_queue
        caller = VerifyCaller(
            namespace=info.namespace,
            subject=subject or info.workflow_type,
            task_queue=info.task_queue,
            workflow_id=info.workflow_id,
            run_id=info.run_id,
        )
        builder = builders.get(ctx.tool_name)
        if builder is None:
            # Tool arguments may be pydantic models; the policy sees their JSON.
            built = PolicyCalls(arg0=to_jsonable_python(ctx.tool_input))
        else:
            built = await builder(ctx)
        client = workflow.create_nexus_client(service=service, endpoint=endpoint)
        # Concurrent evaluations may send the same events; the verifier deduplicates them.
        start = recorded.get(info.run_id, 0)
        if record_operation is not None and len(ctx.received) > start:
            await client.execute_operation(
                record_operation,
                RecordInputEventsRequest(
                    caller=caller,
                    events=input_events(ctx.received[start:], action_prefix=prefix),
                    from_history_start=start == 0,
                ).model_dump(mode="json", by_alias=True),
                output_type=RecordInputEventsResponse,
                schedule_to_close_timeout=timeout,
                cancellation_type=workflow.NexusOperationCancellationType.ABANDON,
                summary=f"record {len(ctx.received) - start} input events",
            )
            recorded[info.run_id] = max(recorded.get(info.run_id, 0), len(ctx.received))
        call = VerifyToolCall(name=f"{prefix}/{ctx.tool_name}", parameters=[_value(built.arg0)])
        checks = [
            VerifyToolCall(name=f"{prefix}/{check.tool}", parameters=[_value(check.arg0)])
            for check in built.checks
        ]
        request = VerifyToolCallsRequest(caller=caller, tool_calls=[call, *checks])
        response = await client.execute_operation(
            verify_operation,
            request.model_dump(mode="json", by_alias=True),
            output_type=VerifyToolCallsResponse,
            schedule_to_close_timeout=timeout,
            # The verifiers can't cancel a verification, and a superseded evaluation has
            # nothing left to wait for.
            cancellation_type=workflow.NexusOperationCancellationType.ABANDON,
            summary=call.name,
        )
        details: dict[str, Any] = {
            "endpoint": endpoint,
            "service": service,
            "action": call.name,
            "parameter": built.arg0,
            "criteria_set": ctx.criteria_set_name,
            "criteria_version": ctx.criteria_version,
        }
        if checks:
            details["checks"] = [
                {"action": check.name, "parameter": source.arg0}
                for check, source in zip(checks, built.checks, strict=True)
            ]
        if built.explanation:
            details["explanation"] = built.explanation
        decision = decide(response, details=details)
        if decision.verdict is AutoApprovalVerdict.DENY and built.explanation:
            decision.reason = f"{decision.reason}\n\n{built.explanation}"
        return decision

    setattr(evaluate, AUTO_MODE_EVALUATOR_ATTR, "dogwood_evaluator")
    return evaluate


def input_events(received: Sequence[ReceivedInput], *, action_prefix: str) -> list[InputEvent]:
    """``received`` as ``RecordInputEvents`` events.

    A message is an update named after its handler, which Dogwood records as the action
    ``update/<handler>``. A tool result is an activity result named
    ``<action_prefix>/<tool_name>/result``: its own action, since a result's shape isn't the
    call's. Its payloads are the result (``arg0``) and the call's arguments (``arg1``), so a
    policy can tie the result to what was asked.
    """
    events: list[InputEvent] = []
    for item in received:
        payloads = [_value(item.payload)]
        if item.kind is ReceivedInputKind.MESSAGE:
            kind, name, update_id = InputEventKind.UPDATE, item.name, item.id
        else:
            kind = InputEventKind.ACTIVITY_RESULT
            name, update_id = f"{action_prefix}/{item.name}/result", f"tool-result/{item.id}"
            payloads.append(_value(item.arguments))
        events.append(InputEvent(kind=kind, name=name, payloads=payloads, update_id=update_id))
    return events


def _value(value: JsonValue) -> dict[str, JsonValue]:
    return {"@type": _VALUE_TYPE_URL, "value": value}


def decide(
    response: VerifyToolCallsResponse, *, details: Mapping[str, Any] | None = None
) -> AutoApprovalDecision:
    """Map the policy's decision onto a verdict.

    Approved approves, denied denies, and escalate goes to a human: the verifier escalates a
    call when every policy that denied it is annotated ``@decision("escalate")``. Any other
    answer (retry later, no decision, a value this file doesn't know) raises, which also
    escalates, and publishes the evaluation as failed.
    """
    decision = (
        Decision.Name(response.decision)
        if response.decision in Decision.values()
        else str(response.decision)
    )
    if response.decision == DECISION_APPROVED:
        verdict = AutoApprovalVerdict.APPROVE
        reason = response.reason or "approved by the tool policy"
    elif response.decision == DECISION_DENIED:
        verdict = AutoApprovalVerdict.DENY
        reason = response.reason or "denied by the tool policy"
    elif response.decision == DECISION_ESCALATE:
        verdict = AutoApprovalVerdict.ESCALATE
        reason = response.reason or "the tool policy asks a person"
    else:
        raise RuntimeError(f"the tool policy did not decide ({decision}): {response.reason}")
    return AutoApprovalDecision(
        verdict=verdict,
        reason=reason,
        details={**(details or {}), "decision": decision, "policy_reason": response.reason},
    )
