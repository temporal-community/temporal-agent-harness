# ABOUTME: The ``agent`` namespace for authoring agent workflows and their tools.
# Decorate the workflow class with ``@agent.defn`` (a contract-checked ``@workflow.defn``)
# and each tool with ``@agent.activity_tool_defn()`` (durable, activity-backed) or
# ``@agent.tool_defn()`` (inline in the workflow). Each publishes its own
# tool_start/tool_end lifecycle events and can gate execution on a human approval.
#
# Usage::
#
#     from temporalio import workflow
#     from temporalio.workflow import ActivityConfig
#     from harness import agent
#     from harness.agent_protocol import AgentConfig
#
#     @agent.defn   # enforces the agent contract
#     class MyAgent:
#         @workflow.run
#         async def run(self, config: AgentConfig) -> None: ...
#
#     # Durable, activity-backed tool. Register it on the worker via tool_activity():
#     #   Worker(..., activities=[agent.tool_activity(get_page_outline), ...])
#     @agent.activity_tool_defn(
#         activity_config=ActivityConfig(start_to_close_timeout=timedelta(seconds=30)),
#     )
#     async def get_page_outline(page_url: str) -> str: ...
#
#     # Inline tool — runs in the workflow, no activity:
#     @agent.tool_defn()
#     async def summarize(text: str) -> str: ...
#
# Tool approvals are SAFE-BY-DEFAULT and policy-driven. A tool only asserts whether it is
# ``inherently_safe=True`` (never, under any input, unsafe) — a static hint, not a decision.
# Whether a call is actually gated is up to the agent's ``ToolApprovalPolicy`` (set as the
# required ``approval_policy_default=`` runner constructor arg, overridable per
# session via ``AgentConfig.approval_policy``, updatable at runtime via
# ``runner.set_approval_policy``). A gated call pauses in-workflow for a human approve/deny
# (see the ``tool_approval`` update + ``ToolApprovalRequested``/``ToolApprovalResolved``).
#
# Annotate a parameter ``x: Injected[Foo]`` to have the WORKFLOW supply it per call
# (via run_tool(injections=...)) instead of the model — hidden from the model's tool
# schema. Use it for per-call context the model must not choose::
#
#     @agent.activity_tool_defn()
#     async def read_page(store: Injected[str], page_url: str) -> str: ...

from temporal_agent_harness.harness.agent_protocol import ToolApprovalContext, ToolApprovalPolicy
from temporal_agent_harness.harness.agent_workflow import (
    AgentToolContext,
    CustomApprovalFallback,
    Injected,
    ToolApprovalDenied,
    accepts,
    activity_tool_defn,
    defn,
    tool_activity,
    tool_defn,
)
from temporal_agent_harness.harness.subagent_toolset import subagent_toolset

__all__ = [
    "AgentToolContext",
    "CustomApprovalFallback",
    "Injected",
    "ToolApprovalContext",
    "ToolApprovalDenied",
    "ToolApprovalPolicy",
    "accepts",
    "activity_tool_defn",
    "defn",
    "subagent_toolset",
    "tool_activity",
    "tool_defn",
]
