"""The governed analytics agent: everything the harness contributes, in one place.

An analyst asks the bank's data questions; the agent answers by writing Python that composes a
small, fixed set of DuckDB tools. Those tools are its only capabilities: no shell, no files,
no network. Every tool call goes through one durable choke point before it runs:

  1. **Code Mode** (``agent.code_mode_tool``) turns the tools into async host functions of one
     ``run_analysis`` tool. The model writes a script; each host call the script makes runs as
     its own durable, gated tool call.
  2. **The approval policy** (``ToolApprovalPolicy.auto_mode``) puts every gated call to an
     evaluator. Only the script itself and ``request_access`` are pre-approved: a script can do
     nothing but make host calls, each judged on its own, and asking a person needs no
     permission.
  3. **The evaluator** (``dogwood_evaluator``) asks the Dogwood tool policy, a separate
     service over Nexus, whether the call may run (``policies/bank.dw``). Before each question
     it hands the policy what the session received since the last one: the user's messages and
     the tools' results, among them every grant and revocation of access. A SQL tool's call reaches
     the policy as the facts of its query plus one ``read_table`` check per table it reads
     (``sql_policy.py``). The question and the answer are in workflow history.
  4. **Access** (``access.py``) to a restricted table is a person's call, for the rest of the
     session or until they revoke it. The agent asks with ``request_access``, a callback tool:
     the console shows the person the table and the agent's reason, and the person grants or
     declines. Or the person grants it unasked with the ``grant_access`` message, and takes
     it back with ``revoke_access``, mid-turn included. The harness records each with the
     policy, which refuses a restricted table unless it was granted and not revoked since.

The model is Gemini, in ``gemini.py``, which hands each of its tool calls to the runner.
Nothing in this file depends on which model drives it.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        MidTurn,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
    from temporal_agent_harness.harness.dogwood_approvals import dogwood_evaluator

    from examples.governed_analytics.access import (
        AccessRequest,
        TableAccessBoard,
        ask_for_access,
        record_access,
        request_access,
    )
    from examples.governed_analytics.charts import CHART_TOOLS
    from examples.governed_analytics.gemini import GeminiChat
    from examples.governed_analytics.reports import publish_report
    from examples.governed_analytics.sql_policy import SQL_POLICY_CALLS
    from examples.governed_analytics.tools import ALL_TOOLS

TASK_QUEUE = "governed-analytics-agent"
# Tool calls reach the policy as "bank-analyst/<tool>", whatever the task queue.
ACTION_PREFIX = "bank-analyst"
# The Nexus endpoint the Dogwood verifier serves (`just endpoint` creates it).
POLICY_ENDPOINT = "tool-verifier"
CODE_TOOL_NAME = "run_analysis"

# What a script can call: the data tools, asking a person for access, drawing charts, and
# publishing a report. Publishing is judged by the policy like the data tools.
SCRIPT_TOOLS = [*ALL_TOOLS, request_access, *CHART_TOOLS, publish_report]

# Approved without asking the policy. The script can only make host calls, each of which is
# judged; request_access (through ask_for_access) only asks a person; a chart only draws what
# the agent was allowed to read.
PRE_APPROVED_TOOLS = [
    CODE_TOOL_NAME,
    request_access.__name__,
    ask_for_access.__name__,
    *(tool.__name__ for tool in CHART_TOOLS),
]

# Auto mode consults an evaluator only for tools a criteria set covers. With Dogwood deciding,
# one catch-all set with no rules of its own covers them all.
_CRITERIA = agent.AutoApprovalCriteria(
    sets={"dogwood": agent.AutoApprovalCriteriaSet(effect="Judged by the Dogwood tool policy.")},
    default="dogwood",
)


@agent.defn(name="GovernedAnalyticsAgent")
class GovernedAnalyticsAgentWorkflow:
    # Observable state: which restricted tables the agent may read right now, as the console's
    # Agent state panel shows it. The policy decides; this shows where each table stands.
    table_access = agent.state(TableAccessBoard)

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.auto_mode(
                pre_approved_tools=PRE_APPROVED_TOOLS
            ),
            auto_approval_criteria_default=_CRITERIA,
            auto_mode_evaluator=dogwood_evaluator(
                endpoint=POLICY_ENDPOINT,
                action_prefix=ACTION_PREFIX,
                policy_calls=SQL_POLICY_CALLS,
            ),
        )
        # The one tool the model calls: a script over SCRIPT_TOOLS. request_access is handed the
        # access board to update and the runner to ask a person through; both are hidden from
        # the model and the script.
        code_tool = agent.code_mode_tool(
            SCRIPT_TOOLS,
            name=CODE_TOOL_NAME,
            injections={"access": self.table_access, "runner": self._runner},
        )
        self._model = GeminiChat(self._runner, [code_tool])

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the bank's analyst a question about branches, deposits, products or loans. It
        answers by writing and running analysis scripts over the bank's databases; reading
        restricted data needs access you grant."""
        return TextReply(text=await self._model.turn(message.text))

    # Granting and revoking are a person's messages: ACCEPT, so they apply at once, joining a
    # turn already in flight; model_callable=False, so the agent has no tool that sends them.
    # The runner records each one, and the evaluator hands it to the policy before the next
    # check, so neither handler does anything to enforce it: each shows it on the access board
    # and tells the model.

    @agent.accepts(mid_turn=MidTurn.ACCEPT, model_callable=False)
    async def grant_access(self, access: AccessRequest) -> TextReply:
        """Let the agent read a restricted table for the rest of this session, without waiting
        for it to ask. Revoke it with revoke_access."""
        record_access(self.table_access, access.table, "granted", via="your message")
        self._model.tell(f"The user granted you access to {access.table}.")
        return TextReply(text=f"Granted access to `{access.table}` for this session.")

    @agent.accepts(mid_turn=MidTurn.ACCEPT, model_callable=False)
    async def revoke_access(self, access: AccessRequest) -> TextReply:
        """Take back the agent's access to a restricted table, however it was granted. Its
        next query on the table is refused."""
        record_access(self.table_access, access.table, "revoked", via="your message")
        self._model.tell(f"The user revoked your access to {access.table}.")
        return TextReply(text=f"Revoked access to `{access.table}`.")
