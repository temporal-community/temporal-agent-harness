"""Hello-world OpenAI Agents SDK agent with three tools reached over Nexus.

The example now also includes two subagents reached over Nexus/A2A.

    nexus_gateway = nexus_tools_gateway()  # agent_id inferred from workflow_type
    mcp_servers=[
        nexus_gateway.mcp_servers("demo"),
        nexus_native_mcp_server("demo-nexus", "nexus-hello-demo-endpoint"),
    ]

For this demo, the tools are:
  - demo_get_fun_fact: a 3rd-party (non-Nexus) MCP server, proxied through the Durable
    Tools Gateway ("demo" -> http://127.0.0.1:8765/mcp). Registered under agent_id
    "NexusHelloAgent" (this workflow's type) with the gateway ahead of time.
  - demo-nexus_get_lucky_number: a native Nexus tool service, called directly -- no
    gateway, no registration ("demo-nexus" -> "nexus-hello-demo-endpoint").
  - demo-nexus_get_delayed_lucky_number: a workflow-backed Nexus operation, called
    through the same direct service route.

The same direct-vs-gateway split is demonstrated by two A2A subagents: ``research`` is a
harness-native agent reached directly over Nexus, while ``writer`` is an HTTP A2A agent
registered with and routed through the Durable Tools Gateway.

The agent only knows about MCP tools when listing them via the MCP protocol. Subagent tools
are generated from their declared A2A/harness interfaces.
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
        nexus_native_mcp_server,
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
    from .whimsical_workflow import WhimsicalAgentWorkflow

TASK_QUEUE = "nexus-hello"
WORKFLOW_NAME = "NexusHelloAgent"
DEFAULT_MODEL = "gpt-5.1"
ACCOUNT_ID = "NexusHelloAccount"

SYSTEM_INSTRUCTION = """\
You are a friendly assistant. Answer the user in brief, natural prose.
"""

RESEARCH_SUBAGENT_ENDPOINT = "nexus-hello-subagent-endpoint"
RESEARCH_KEY = "research"
WHIMSICAL_SUBAGENT_ENDPOINT = "nexus-hello-whimsical-agent-endpoint"
WHIMSICAL_KEY = "whimsical-agent"
WRITER_KEY = "writer"
WRITER_ALIAS = "writer"


@workflow.defn(name=WORKFLOW_NAME)
@agent.defn
class NexusHelloAgentWorkflow:
    """A conversational agent with three tools reached over Nexus.

    It can also delegate to one native and one gateway-routed A2A subagent.
    """

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
        """Ask the agent a question; it may use its Nexus-brokered tools to answer.

        The model may also delegate parts of the question to its A2A subagents.
        """
        account_gateway = nexus_gateway(ACCOUNT_ID)
        subagent_gateway = agent.nexus_subagent_gateway(ACCOUNT_ID)

        research_tools = agent.nexus_native_subagent(
            NativeResearchSubagentWorkflow, RESEARCH_SUBAGENT_ENDPOINT, key=RESEARCH_KEY
        )
        whimsical_tools = agent.nexus_native_subagent(
            WhimsicalAgentWorkflow,
            WHIMSICAL_SUBAGENT_ENDPOINT,
            key=WHIMSICAL_KEY,
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
                for fn in [*research_tools, *whimsical_tools, *writer_tools]
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
