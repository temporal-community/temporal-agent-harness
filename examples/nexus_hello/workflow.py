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
        """Ask the agent a question; it may use its Nexus-brokered tools to answer.

        The model may also delegate parts of the question to any account-owned A2A agent made
        available through the dynamically resolved toolbox.
        """
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
            tools=[harness_tool_as_openai_tool(fn) for fn in toolbox.subagent_tools],
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
