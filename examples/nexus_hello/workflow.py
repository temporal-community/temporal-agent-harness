"""Hello-world OpenAI Agents SDK agent with two tools reached over Nexus.

    nexus_gateway = nexus_tools_gateway()  # agent_id inferred from workflow_type
    mcp_servers=[
        nexus_gateway.mcp_servers("demo"),
        nexus_native_mcp_server("demo-nexus", "nexus-hello-demo-endpoint"),
    ]

For this demo, the tools are:
  - demo_get_fun_fact: a 3rd-party (non-Nexus) MCP server, proxied through the Durable
    Tools Gateway ("demo" -> http://127.0.0.1:8765/mcp). Registered under agent_id
    "NexusHelloAgent" (this workflow's type) with the gateway ahead of time.
  - demo-nexus_get_lucky_number: a Nexus-native MCP server, called directly -- no
    gateway, no registration ("demo-nexus" -> "nexus-hello-demo-endpoint").

The agent only knows about them when listing tools via the MCP protocol.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import Runner, TResponseInputItem

    from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
        nexus_native_mcp_server,
        nexus_tools_gateway,
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
DEFAULT_MODEL = "gpt-5.1"

SYSTEM_INSTRUCTION = """\
You are a friendly assistant. Answer the user in brief, natural prose.
"""


@workflow.defn(name="NexusHelloAgent")
@agent.defn
class NexusHelloAgentWorkflow:
    """A conversational agent with two tools reached over Nexus."""

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
        """Ask the agent a question; it may use its Nexus-brokered tools to answer."""
        nexus_gateway = nexus_tools_gateway()
        sdk_agent = OpenAIAgent(
            name="NexusHello",
            instructions=SYSTEM_INSTRUCTION,
            model=DEFAULT_MODEL,
            mcp_servers=[
                nexus_gateway.mcp_servers("demo"),
                nexus_native_mcp_server("demo-nexus", "nexus-hello-demo-endpoint"),
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
