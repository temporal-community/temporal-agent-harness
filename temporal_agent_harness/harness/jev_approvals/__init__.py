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

The package splits along the Temporal workflow boundary, so importing it never requires the
optional ``jev`` extra:

  * Workflow-safe (this ``__init__`` and the :mod:`.approver` / :mod:`.models` modules):
    safe to import anywhere, including inside a workflow. It composes the question as plain
    dicts and dispatches the model call as an activity BY NAME.
  * Worker-side (:mod:`.activity`): the one activity that actually talks to TypeSafe.
    ``AgentHarnessPlugin`` registers it on every worker (a worker can also register
    ``JEV_APPROVAL_ACTIVITIES`` from that module by hand). Nothing here imports it, so the
    workflow-safe surface stays free of the SDK. It imports no ``typesafe_sdk`` itself
    either — it checks that the ``jev`` extra is installed and then delegates — so a worker
    missing the extra fails one approval check, non-retryably, and the call escalates to a
    human instead of hanging on Temporal's retryable "activity not registered".

Install the extra on the worker::

    uv add 'temporal-agent-harness[jev]'    # or: pip install 'temporal-agent-harness[jev]'

and give that worker a ``TYPESAFE_API_KEY``.
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
