"""Auto mode for tool approvals: let Jev decide whether a gated tool call may run.

``jev_evaluator()`` returns an ``auto_mode_evaluator`` for
:class:`~temporal_agent_harness.harness.agent_workflow.AgentWorkflowRunner`. It is the
MECHANISM only — it carries no rules and no thresholds. Those are the operator's
:class:`~temporal_agent_harness.harness.agent_protocol.AutoApprovalCriteria`, which arrive
on every :class:`AutoApprovalContext`, so swapping this evaluator for an LLM-backed one
strands none of an operator's configuration.

It is reached only for a call that the agent's
:class:`~temporal_agent_harness.harness.agent_protocol.ToolApprovalPolicy` did not already
auto-approve AND whose policy has ``auto_mode_enabled`` — auto mode is a mode the
operator switches on, not a fallback that activates because an evaluator was wired. The
verdict comes back ``approve``, ``deny``, or ``escalate``, the last of which leaves the
call in the human gate exactly where it would have been anyway.

TypeSafe is included in the base harness install. Register a configured
``temporalio.typesafe.TypeSafePlugin`` before ``AgentHarnessPlugin`` on the worker,
using an ``AsyncTypeSafeClient`` with ``RetryPolicy(max_retries=0)``. The canonical
plugin owns provider execution, response decoding and error translation. This evaluator
builds the approval questions and applies the operator's thresholds.

A workflow patch retains the original activity and answer type for pre-migration histories.
``AgentHarnessPlugin`` also registers a missing-provider fallback: without ``TypeSafePlugin``,
the evaluation fails non-retryably with registration guidance and reaches the human gate.
Credentials and client configuration stay worker-side.
"""

from .approver import (
    DEFAULT_ACTIVITY_CONFIG,
    build_request,
    decide,
    jev_evaluator,
)
from .models import (
    DEFAULT_JEV_MODEL,
    JEV_TOOL_APPROVAL_ACTIVITY,
    JevApprovalAnswer,
    JevApprovalRequest,
)

__all__ = [
    "DEFAULT_ACTIVITY_CONFIG",
    "DEFAULT_JEV_MODEL",
    "JEV_TOOL_APPROVAL_ACTIVITY",
    "JevApprovalAnswer",
    "JevApprovalRequest",
    "build_request",
    "decide",
    "jev_evaluator",
]
