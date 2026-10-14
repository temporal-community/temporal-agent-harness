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

The Jev call is a typed ``temporalio.typesafe`` System One call, so install the extra and
register ``TypeSafePlugin`` (before ``AgentHarnessPlugin``) on the worker::

    uv add 'temporal-agent-harness[typesafe]'

    async with AsyncTypeSafeClient(retry=RetryPolicy(max_retries=0)) as typesafe:
        client = await Client.connect(
            ..., plugins=[TypeSafePlugin(typesafe), AgentHarnessPlugin(...)]
        )
"""

from .evaluator import (
    DEFAULT_ACTIVITY_CONFIG,
    DEFAULT_JEV_MODEL,
    JevApprovalAnswer,
    JevApprovalRequest,
    build_request,
    decide,
    jev_evaluator,
)

__all__ = [
    "DEFAULT_ACTIVITY_CONFIG",
    "DEFAULT_JEV_MODEL",
    "JevApprovalAnswer",
    "JevApprovalRequest",
    "build_request",
    "decide",
    "jev_evaluator",
]
