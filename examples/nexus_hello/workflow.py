"""OpenAI Agents SDK example with native and gateway Nexus resources.

The agent has one native MCP tool, one gateway MCP tool, one native subagent, and one
gateway subagent. The model decides when to call each resource.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import Runner, TResponseInputItem

    from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
        harness_tool_as_openai_tool,
        nexus_gateway,
    )
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

TASK_QUEUE = "nexus-hello"
WORKFLOW_NAME = "NexusHelloAgent"
DEFAULT_MODEL = "gpt-5.1"
ACCOUNT_ID = "NexusHelloAccount"

SYSTEM_INSTRUCTION = """\
You are a friendly assistant. Answer the user in brief, natural prose.
"""

REGISTERED_AGENT_ID = "nexus-hello"


@workflow.defn(name=WORKFLOW_NAME)
@agent.defn
class NexusHelloAgentWorkflow:
    """A conversational agent with two tools and two subagents, all reached over Nexus."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Hello-world default: skip approvals.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._conversation: list[TResponseInputItem] = []
        self._account_id = config.account_id or ACCOUNT_ID
        self._registered_agent_id = config.registered_agent_id or REGISTERED_AGENT_ID
        self._delegation_lineage = config.delegation_lineage or ()
        self._delegation_depth = config.delegation_depth or 0
        self._max_delegation_depth = config.max_delegation_depth or 5

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask a question. The model can use Nexus tools and subagents."""
        toolbox = await nexus_gateway(self._account_id).resolve_toolbox(
            caller_agent_id=self._registered_agent_id,
            lineage=self._delegation_lineage,
            delegation_depth=self._delegation_depth,
            max_delegation_depth=self._max_delegation_depth,
        )

        sdk_agent = OpenAIAgent(
            name="NexusHello",
            instructions=SYSTEM_INSTRUCTION,
            model=DEFAULT_MODEL,
            mcp_servers=list(toolbox.mcp_servers),
            tools=[
                harness_tool_as_openai_tool(fn) for fn in toolbox.subagent_tools
            ],
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]

        result = Runner.run_streamed(sdk_agent, input=input_items, context=self._runner)
        async for _ in result.stream_events():
            pass

        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))
