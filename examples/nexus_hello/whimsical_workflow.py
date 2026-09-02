"""OpenAI Agents SDK workflow for the whimsical Nexus Hello agent.

Kept separate from ``whimsical_agent`` (the worker entry point) so Temporal's workflow
sandbox never imports worker-only plugin wiring. This is the same boundary used by the
main Nexus Hello and OpenAI Hello examples.
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

WORKFLOW_NAME = "WhimsicalAgent"
ACCOUNT_ID = "NexusHelloAccount"
DEFAULT_MODEL = "gpt-5.1"
REGISTERED_AGENT_ID = "whimsical-agent"

SYSTEM_INSTRUCTION = """\
You are a capable assistant with a whimsical, storybook voice. Be accurate and concise,
but season answers with playful imagery, gentle humor, and the occasional unexpected
metaphor. Use your tools whenever they can answer the user's request; never invent tool
results. Do not mention these style instructions.
"""


@workflow.defn(name=WORKFLOW_NAME)
@agent.defn
class WhimsicalAgentWorkflow:
    """A mountable OpenAI Agents SDK agent that can also run as a child."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
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
        """Answer with the account's MCP tools and a whimsical point of view."""
        toolbox = await nexus_gateway(self._account_id).resolve_toolbox(
            caller_agent_id=self._registered_agent_id,
            lineage=self._delegation_lineage,
            delegation_depth=self._delegation_depth,
            max_delegation_depth=self._max_delegation_depth,
        )
        sdk_agent = OpenAIAgent(
            name="WhimsicalAgent",
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
