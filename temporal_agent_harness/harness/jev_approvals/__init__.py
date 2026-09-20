"""Auto mode for tool approvals: let Jev decide whether a gated tool call may run.

``jev_evaluator(policy=...)`` returns a ``auto_approval_evaluator`` for
:class:`~temporal_agent_harness.harness.agent_workflow.AgentWorkflowRunner`. Every call the
agent's :class:`~temporal_agent_harness.harness.agent_protocol.ToolApprovalPolicy` does not
already auto-approve is put to Jev against the operator's rules, written once in plain
language, and comes back ``approve``, ``deny``, or ``escalate`` — the last of which leaves
the call in the human gate exactly where it would have been anyway.

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
    DEFAULT_IRREVERSIBLE_CEILING,
    DEFAULT_MIN_CONFIDENCE,
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
    "DEFAULT_IRREVERSIBLE_CEILING",
    "DEFAULT_JEV_MODEL",
    "DEFAULT_MIN_CONFIDENCE",
    "JEV_TOOL_APPROVAL_ACTIVITY",
    "JevApprovalAnswer",
    "JevApprovalRequest",
    "build_request",
    "decide",
    "jev_evaluator",
]
