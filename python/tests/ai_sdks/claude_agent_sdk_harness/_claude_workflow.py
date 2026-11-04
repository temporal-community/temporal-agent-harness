# ABOUTME: A harness agent that drives Claude Code with the harness's Claude parts, for the
# end-to-end tests. Claude Code may use Bash, Read, and harness tools, and can delegate to one
# subagent, which may use Bash and shout, through its Agent tool. Every call goes to a person. Its
# transport gets a connect timeout and a topic for Claude Code's stream events, which the tests check.

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions

    from temporal_agent_harness.ai_sdks.claude_agent_sdk_harness import (
        HarnessClaudeSession,
    )
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

# The connect Activity's timeout, and the topic for Claude Code's stream events.
CONNECT_TIMEOUT = timedelta(minutes=7)
CLAUDE_STREAM_TOPIC = "claude-stream-events"

# How long the other Activities wait for the Worker that runs Claude Code, so a test that stops
# that Worker reconnects in seconds instead of the plugin's default 30.
WORKER_GONE = {"schedule_to_start_timeout": timedelta(seconds=5)}

OPTIONS = ClaudeAgentOptions(
    agents={
        "writer": AgentDefinition(
            description="Writes files.",
            prompt="You write files.",
            tools=["Bash", "mcp__harness__shout"],
        ),
    },
    tools=["Agent", "Bash", "Read"],
    setting_sources=[],
)


@agent.tool_defn()
async def shout(text: str) -> str:
    """Shout the text."""
    return text.upper()


@agent.callback_tool_defn()
async def ask_user(question: str) -> str:
    """Ask the user a question."""
    ...


@agent.defn(name="ClaudeSessionTestAgent")
class ClaudeSessionTestAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        stream = WorkflowStream()
        self._runner = AgentWorkflowRunner(
            config,
            stream=stream,
            approval_policy_default=ToolApprovalPolicy.always_require_human_approval(),
        )
        self._claude = HarnessClaudeSession(
            self._runner,
            OPTIONS,
            [shout, ask_user],
            activity_options={
                "connect": {"start_to_close_timeout": CONNECT_TIMEOUT},
                "write": WORKER_GONE,
                "read": WORKER_GONE,
                "end_input": WORKER_GONE,
                "close": WORKER_GONE,
            },
            stream=stream.topic(CLAUDE_STREAM_TOPIC),
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask Claude Code."""
        result = await self._claude.run(message.text)
        return TextReply(text=result.result or "")
