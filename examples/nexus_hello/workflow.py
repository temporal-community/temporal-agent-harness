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
        nexus_native_mcp_server,
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

    from .native_subagent import NativeResearchSubagentWorkflow

TASK_QUEUE = "nexus-hello"
DEFAULT_MODEL = "gpt-5.1"
ACCOUNT_ID = "NexusHelloAccount"

SYSTEM_INSTRUCTION = """\
You are a friendly assistant. Answer the user in brief, natural prose.
"""

RESEARCH_SUBAGENT_ENDPOINT = "nexus-hello-subagent-endpoint"
RESEARCH_KEY = "research"
WRITER_KEY = "writer"
WRITER_ALIAS = "writer"


@workflow.defn(name="NexusHelloAgent")
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

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask a question. The model can use Nexus tools and subagents."""
        account_gateway = nexus_gateway(ACCOUNT_ID)
        subagent_gateway = agent.nexus_subagent_gateway(ACCOUNT_ID)

        research_tools = agent.nexus_native_subagent(
            NativeResearchSubagentWorkflow, RESEARCH_SUBAGENT_ENDPOINT, key=RESEARCH_KEY
        )
        writer_tools = subagent_gateway.subagent(
            [
                agent.declared_handler(
                    "ask",
                    "Ask the writer subagent a question.",
                    TextMessage,
                    TextReply,
                    param_name="message",
                )
            ],
            WRITER_ALIAS,
            key=WRITER_KEY,
        )

        sdk_agent = OpenAIAgent(
            name="NexusHello",
            instructions=SYSTEM_INSTRUCTION,
            model=DEFAULT_MODEL,
            mcp_servers=[
                account_gateway.mcp_servers("demo"),
                nexus_native_mcp_server("demo-nexus", "nexus-hello-demo-endpoint"),
            ],
            tools=[
                harness_tool_as_openai_tool(fn)
                for fn in [*research_tools, *writer_tools]
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
