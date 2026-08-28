"""Hello-world OpenAI Agents SDK agent with two tools and two subagents, all reached over
Nexus:

    nexus_gateway = nexus_tools_gateway()  # agent_id inferred from workflow_type
    mcp_servers=[
        nexus_gateway.mcp_servers("demo"),
        nexus_native_mcp_server("demo-nexus", "nexus-hello-demo-endpoint"),
    ]

    research = agent.nexus_native_subagent(NativeResearchSubagentWorkflow, "nexus-hello-subagent-endpoint", key="research")
    writer = agent.nexus_subagent_gateway().subagent([...], "writer", key="writer")

For this demo, the resources are:
  - demo_get_fun_fact: a 3rd-party (non-Nexus) MCP server, proxied through the Durable
    Tools Gateway ("demo" -> http://127.0.0.1:8765/mcp). Registered under agent_id
    "NexusHelloAgent" (this workflow's type) with the gateway ahead of time.
  - demo-nexus_get_lucky_number: a Nexus-native MCP server, called directly -- no
    gateway, no registration ("demo-nexus" -> "nexus-hello-demo-endpoint").
  - research: a harness agent (a Nexus-native SUBAGENT, native_subagent.py), reached
    directly over Nexus -- no gateway, no registration.
  - writer: a 3rd-party (non-harness) SUBAGENT (subagent_server.py, plain HTTP), proxied
    through the SAME Durable Tools Gateway as "demo" -- registered under agent_id
    "NexusHelloAgent" ahead of time.

The two subagents demonstrate that the gateway and the native-Nexus path both generalize
beyond MCP tools to a different resource kind (a whole subagent, not a tool). The model
only ever sees the two MCP tools via the MCP protocol; the subagents are driven directly
(no model in that half), so their replies are appended to the model's own reply.
"""

from __future__ import annotations

import asyncio

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

    from .native_subagent import NativeResearchSubagentWorkflow

TASK_QUEUE = "nexus-hello"
DEFAULT_MODEL = "gpt-5.1"

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
        """Ask the agent a question; it may use its Nexus-brokered tools to answer. The
        same turn also asks two Nexus-brokered SUBAGENTS the same question directly (no
        model in that half) and appends their replies."""
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
        model_reply = str(result.final_output)

        research_reply, writer_reply = await self._ask_subagents(message.text)
        return TextReply(
            text=(
                f"{model_reply}\n\n"
                f"--- subagents (same gateway + same native-Nexus path, different resource kind) ---\n"
                f"{RESEARCH_KEY} (native Nexus subagent): {research_reply}\n"
                f"{WRITER_KEY} (gateway-brokered subagent): {writer_reply}"
            )
        )

    async def _ask_subagents(self, text: str) -> tuple[str, str]:
        """Start both subagents, ask each the same question, stop both. No model involved --
        this is a direct exercise of nexus_native_subagent / nexus_subagent_gateway."""
        research_tools = {
            t.__name__: t
            for t in agent.nexus_native_subagent(
                NativeResearchSubagentWorkflow, RESEARCH_SUBAGENT_ENDPOINT, key=RESEARCH_KEY
            )
        }
        gateway = agent.nexus_subagent_gateway()
        writer_tools = {
            t.__name__: t
            for t in gateway.subagent(
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
        }

        research_handle, writer_handle = await asyncio.gather(
            self._runner.run_tool("start-research", research_tools[f"start_{RESEARCH_KEY}"]),
            self._runner.run_tool("start-writer", writer_tools[f"start_{WRITER_KEY}"]),
        )
        try:
            research_reply, writer_reply = await asyncio.gather(
                self._runner.run_tool(
                    "research-ask",
                    research_tools[f"{RESEARCH_KEY}_ask"],
                    subagent=research_handle,
                    message={"text": text},
                ),
                self._runner.run_tool(
                    "writer-ask",
                    writer_tools[f"{WRITER_KEY}_ask"],
                    subagent=writer_handle,
                    message={"text": text},
                ),
            )
        finally:
            await asyncio.gather(
                self._runner.run_tool(
                    "stop-research",
                    research_tools[f"stop_{RESEARCH_KEY}"],
                    subagent=research_handle,
                ),
                self._runner.run_tool(
                    "stop-writer", writer_tools[f"stop_{WRITER_KEY}"], subagent=writer_handle
                ),
            )
        return research_reply.text, writer_reply.text
