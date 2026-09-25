# ABOUTME: The ``agent`` namespace for authoring agent workflows and their tools.
# Decorate the workflow class with ``@agent.defn`` (a contract-checked ``@workflow.defn``,
# used in its place) and each tool with ``@agent.activity_tool_defn()`` (durable,
# activity-backed) or ``@agent.tool_defn()`` (inline in the workflow). Each publishes its own
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
# AUTO MODE is that policy's fourth layer: with ``auto_mode_enabled`` a gated call is first
# put to the runner's ``auto_mode_evaluator=`` (``agent.jev_evaluator()`` is the builtin),
# which may approve it, deny it, or escalate it to the human gate anyway. OFF by default, so
# wiring an evaluator alone changes nothing — an operator wanting only their static allow-list
# plus their own eyes leaves it off. A tool names the criteria set it should be judged against::
#
#     @agent.activity_tool_defn(auto_approval_criteria="financial")
#     async def issue_refund(order_id: str, amount_cents: int) -> Receipt: ...
#
# The rules behind that NAME are ``AutoApprovalCriteria`` — configuration, not code, so an
# operator overrides them per session (``AgentConfig.auto_approval_criteria``) or at runtime
# (``runner.set_auto_approval_criteria`` / ``runner.assign_tool_criteria``), and the same
# assignment-by-name covers tools with no decorator at all (an MCP server's, say).
#
# Declare accepted messages as ``@agent.accepts`` handler methods. Each declares its own
# ``mid_turn`` (what happens if the message arrives while a turn is open — queue, fail, or
# join it) and a ``model_callable`` hint (whether a PARENT agent's model may drive it; the
# parent's ``SubagentToolPolicy`` decides for real). See ``MidTurn``.
#
# Declare observable state as a class attribute, ``plan = agent.state(PlanState)``: the
# attribute name is its id, ``self.plan`` is the instance's ``StateRef``, and every committed
# ``with self.plan.mutate() as d:`` is published to the event stream as JSON Patch ops.
#
# Annotate a parameter ``x: Injected[Foo]`` to have the WORKFLOW supply it per call
# (via run_tool(injections=...)) instead of the model — hidden from the model's tool
# schema. Use it for per-call context the model must not choose::
#
#     @agent.activity_tool_defn()
#     async def read_page(store: Injected[str], page_url: str) -> str: ...

from temporal_agent_harness.harness.agent_protocol import (
    AutoApprovalCriteria,
    AutoApprovalCriteriaSet,
    AutoApprovalVerdict,
    AutoApprovalDecision,
    MessageContext,
    MidTurn,
    AutoApprovalContext,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_workflow import (
    AgentToolContext,
    CallbackToolError,
    AutoModeEvaluator,
    Injected,
    ToolApprovalDenied,
    WorkflowDefnOptions,
    accepts,
    activity_tool_defn,
    callback_tool_defn,
    defn,
    tool_activity,
    tool_defn,
)
from temporal_agent_harness.harness.code_mode import code_mode_tool
from temporal_agent_harness.harness.state.decl import StateDecl, state
from temporal_agent_harness.harness.jev_approvals import jev_evaluator
from temporal_agent_harness.harness.subagent_toolset import (
    SubagentToolPolicy,
    subagent_toolset,
)

__all__ = [
    "AgentToolContext",
    "AutoApprovalCriteria",
    "AutoApprovalCriteriaSet",
    "AutoApprovalVerdict",
    "AutoApprovalDecision",
    "CallbackToolError",
    "AutoModeEvaluator",
    "Injected",
    "MessageContext",
    "MidTurn",
    "AutoApprovalContext",
    "ToolApprovalDenied",
    "ToolApprovalPolicy",
    "WorkflowDefnOptions",
    "accepts",
    "activity_tool_defn",
    "callback_tool_defn",
    "code_mode_tool",
    "defn",
    "jev_evaluator",
    "StateDecl",
    "SubagentToolPolicy",
    "state",
    "subagent_toolset",
    "tool_activity",
    "tool_defn",
]
