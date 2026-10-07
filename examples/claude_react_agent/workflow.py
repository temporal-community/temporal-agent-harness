"""The ReAct example, on Claude: the same weather / geo / IP tools and ``ask_user`` callback tool as
``examples/react_agent``, but the model loop is the Claude Agent SDK (``temporalio.claude_agent_sdk``)
instead of the OpenAI Agents SDK. The tools are unchanged harness tools, so approvals and
``tool_start`` / ``tool_end`` / ``callback_requested`` events behave exactly as in the OpenAI version.
Left out: the Formula 1 MCP server (it is wired through the OpenAI MCP provider).

EXPLORATORY: needs temporalio/ai-integrations#33. See docs/design/claude-agent-sdk-integration.md.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.ai_sdks.claude_agent_sdk_harness import HarnessClaudeAgent
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from examples.react_agent.human_tools import HUMAN_TOOLS
    from examples.react_agent.tool_activities import ALL_TOOLS

TASK_QUEUE = "claude-react-agent"

SYSTEM_PROMPT = """\
You are a helpful location and weather assistant. Answer the user in brief, natural prose.

Use your tools rather than guessing. You can find the weather two ways:
 - For a named city: look up its coordinates with `get_coordinates`, then call `get_weather`.
 - For the user's current location: call `get_ip_address`, then `get_location_info` (to get
   coordinates from the IP), then `get_weather`.
Chain tools as needed, then reply in a sentence or two. `get_weather` returns the temperature in
Fahrenheit, a weather code, and wind speed; summarize it in plain language.

If a request is ambiguous or needs information only the user can give — which city they mean, or
permission to use their current location — call `ask_user` with a clear question and use the
answer. Prefer asking over guessing when it matters."""


@agent.defn(name="ClaudeReactAgent")
class ClaudeReactAgentWorkflow:
    """The ReAct weather/geo agent, driven by the Claude Agent SDK."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._claude: HarnessClaudeAgent | None = None

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant. Ask about the weather in a city, or where you are, and it
        chains its tools (coordinates / IP-location / weather) and tells you what it found."""
        if self._claude is None:
            self._claude = HarnessClaudeAgent(
                self._runner,
                system_prompt=SYSTEM_PROMPT,
                tools=[*ALL_TOOLS, *HUMAN_TOOLS],
            )
        return TextReply(text=await self._claude.run(message.text))
