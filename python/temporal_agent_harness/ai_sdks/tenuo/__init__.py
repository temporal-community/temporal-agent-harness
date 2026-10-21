"""Auto mode for tool approvals: check each gated call against the session's Tenuo warrant.

``tenuo_evaluator()`` returns an ``auto_mode_evaluator`` for
:class:`~temporal_agent_harness.harness.agent_workflow.AgentWorkflowRunner`. The warrant is
issued outside the agent for one task and arrives in the session's init data. Calls it doesn't
allow are denied, calls it allows are approved, and calls it gates wait for approvals signed by
the approvers the warrant names, through the worker's Tenuo approval handler. The evaluator
never escalates to the harness's approval UI.

It is configured with the ``TenuoPluginConfig`` from Tenuo's Temporal integration, so trusted
roots, keys, revocation, ``tool_mappings``, observe mode (``dry_run``) and the approval handler
work as they do there. Install the extra, build one config on the worker, then build the
evaluator from it in ``@agent.init``::

    uv add 'temporal-agent-harness[tenuo]'

    # worker configuration, imported passed through the workflow sandbox
    TENUO = TenuoPluginConfig(
        signing_key=HOLDER_KEY,
        trusted_roots=[ISSUER_ROOT],
        approval_handler=cli_prompt(approver_key=APPROVER_KEY),  # or Tenuo Cloud, below
    )

    @agent.init
    def __init__(self, config: AgentConfig, task: RefundTask) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.auto_mode(),
            auto_approval_criteria_default=warrant_criteria(),
            auto_mode_evaluator=tenuo_evaluator(warrant=task.warrant, config=TENUO),
        )

    Worker(..., activities=TenuoApprovalActivities(TENUO).all())

For approvals in the Tenuo Cloud dashboard or Slack, build the config from
``tenuo_cloud.temporal`` instead, with ``TENUO_API_KEY`` set; it wires the approval handler.

Import this module inside ``workflow.unsafe.imports_passed_through()``, like the other
``ai_sdks`` modules: Tenuo is a native extension.
"""

from temporal_agent_harness.ai_sdks.tenuo.approvals import (
    ApprovalOutcome,
    TenuoApprovalActivities,
    approval_request,
)
from temporal_agent_harness.ai_sdks.tenuo.evaluator import (
    WARRANT_CRITERIA_SET,
    build_authorizer,
    decide,
    tenuo_evaluator,
    warrant_criteria,
)

__all__ = [
    "WARRANT_CRITERIA_SET",
    "ApprovalOutcome",
    "TenuoApprovalActivities",
    "approval_request",
    "build_authorizer",
    "decide",
    "tenuo_evaluator",
    "warrant_criteria",
]
