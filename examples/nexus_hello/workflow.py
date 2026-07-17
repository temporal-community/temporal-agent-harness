"""A hello-world OpenAI Agents SDK agent whose tools are reached through the Nexus-transport
MCP server — entirely opaquely. This workflow's own code never mentions
``nexus_transport_mcp_server`` or ``mcp_servers``; ``worker.py`` configures
``OpenAIAgentsPlugin(nexus_transport=True)`` once, and every ``Agent`` constructed here
automatically gets a working Nexus-transport MCP server appended — see
``TemporalOpenAIRunner._prepare_workflow_run``.

Both tools live OUTSIDE this workflow, reached uniformly via Nexus but registered two
different ways — both through the exact same self-serve registry (``NexusMcpServerRegistry``);
WorkflowTransport tells them apart on its own, from what each one's own list_tools returns,
so there's nothing else to declare either way:

  - demo_get_fun_fact — a 3rd-party (non-Nexus) MCP server, reached *through* the Durable
    Tools Gateway. The gateway is registered against THIS running agent workflow's own
    registry the same self-serve way as demo-nexus below (`just register-gateway
    <this-workflow-id>` — a plain signal, per *session*, sent live while this workflow is
    already running) — deliberately NOT auto-registered at startup, so a session can be used
    to reproduce/test what happens BEFORE the gateway is reachable (e.g. an unregistered-tool
    error) as well as after. WorkflowTransport falls back to it (the gateway's
    RegistryService.call_tool, which starts a plain workflow wrapping one activity in the
    "gateway" namespace) for any tool name that isn't a registered direct server.
  - demo-nexus_get_lucky_number — a Nexus-native MCP server, registered directly against
    THIS running agent workflow's own registry (`just register-nexus-tool
    <this-workflow-id>` — a plain signal, per *session*, sent live while this workflow is
    already running). WorkflowTransport calls it straight through
    workflow.create_nexus_client(), one Temporal hop, straight into the "nexus-mcp-server"
    namespace.

Neither is called directly from this workflow's own code — WorkflowTransport does the routing,
uniformly, for both.

Must be declared @workflow.defn(sandboxed=False): the auto-injected transport runs a live MCP
ClientSession in-workflow (via mcp + anyio), which Temporal's default sandboxed runner can't
support (anyio's asyncio backend subclasses threading.Thread at import time). See
nexus_transport_mcp_server's docstring for the full explanation — this mirrors nexus_mcp's own
ToolCallWorkflow / ToolRegistryWorkflow, which are sandboxed=False for the same reason.

Run it with the shared example stack; see README.md for the full multi-process runbook.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

from agents import Agent as OpenAIAgent
from agents import Runner, TResponseInputItem

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


@workflow.defn(name="NexusHelloAgent", sandboxed=False)
@agent.defn
class NexusHelloAgentWorkflow:
    """A conversational agent whose tools are reached via the Nexus-transport MCP server —
    wired in entirely by worker.py's OpenAIAgentsPlugin(nexus_transport=True)."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Hello-world stance: don't gate tool calls (this MCP tool bypasses the harness
            # approval gate entirely regardless — see the openai-agents-nexus-transport
            # integration notes).
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            # MCP server visibility is opt-in by default (AgentConfig.enabled_mcp_servers) —
            # this demo opts its own two services in so it works out of the box; a real
            # deployment would instead let its caller pass enabled_mcp_servers on AgentConfig
            # (or widen it live via the set_enabled_mcp_servers update).
            enabled_mcp_servers_default=["demo", "demo-nexus"],
        )
        self._conversation: list[TResponseInputItem] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        # There is no separate, worker-level "gateway" concept -- the gateway registers
        # against this workflow's own registry exactly the way any other Nexus-reachable
        # service does (see NexusMcpServerRegistry), under its own real Nexus service name.
        # Deliberately NOT done here at startup (unlike an earlier version of this example) --
        # both tools are opted into the same live, per-session `register_mcp_server` signal
        # (`just register-gateway <this-workflow-id>` / `just register-nexus-tool
        # <this-workflow-id>`, see README.md), so a session can be used to exercise what
        # happens both before and after each one becomes reachable.
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant. Ask for a fun fact about a topic and it calls its
        demo_get_fun_fact tool (reached via the Nexus-transport MCP server) and tells you
        what it found."""
        sdk_agent = OpenAIAgent(
            name="NexusHello",
            instructions=SYSTEM_INSTRUCTION,
            model=DEFAULT_MODEL,
            # No mcp_servers=[...] here -- the plugin appends the Nexus-transport MCP
            # server automatically (see worker.py).
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]

        result = Runner.run_streamed(sdk_agent, input=input_items, context=self._runner)
        async for _event in result.stream_events():
            pass

        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))
