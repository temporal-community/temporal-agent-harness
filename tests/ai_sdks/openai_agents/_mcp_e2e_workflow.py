# ABOUTME: The agent workflow for test_mcp_streaming_e2e. Lives in its own module because
# that test runs under the WORKFLOW SANDBOX, which re-imports the workflow's module -- a
# pytest test module is not safe to import that way.

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import Runner

    from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
        stateless_mcp_server,
    )
    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent import ToolApprovalPolicy
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
    )

MCP_SERVER_NAME = "probe"
MCP_TOOL_NAME = "lookup"
# The call id the fake model streams. tool_requested, tool_start and tool_end must all
# carry it, or the UI draws two unrelated cards for one call.
CALL_ID = "call_stream_1"


@workflow.defn
@agent.defn
class McpStreamingAgent:
    """An agent whose only tool comes from an MCP server. Nothing here wires up
    approvals or tool events -- the harness must supply both on its own."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask the agent something; it answers with its MCP tool."""
        sdk_agent = OpenAIAgent(
            name="McpStreaming",
            instructions="Use the tool.",
            model="gpt-5.1",
            mcp_servers=[stateless_mcp_server(MCP_SERVER_NAME)],
        )
        result = Runner.run_streamed(sdk_agent, input=message.text, context=self._runner)
        async for _ in result.stream_events():
            pass
        return TextReply(text=str(result.final_output))
