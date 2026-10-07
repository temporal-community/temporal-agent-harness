"""A hello-world Claude Agent SDK agent on the harness, with one tool and gated Bash.

Claude runs through ``temporalio.claude_agent_sdk`` (each model step is an Activity, the
conversation lives in the Workflow). The harness owns everything around it: ``get_weather`` is
a normal harness tool, and ``Bash`` — a Claude Code tool the plugin runs as its own Activity —
is gated by the same ``ToolApprovalPolicy`` and publishes the same ``tool_start`` / ``tool_end``
events. See ``docs/design/claude-agent-sdk-integration.md``.

EXPLORATORY: needs temporalio/ai-integrations#33 (not merged) installed.
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


TASK_QUEUE = "claude-hello"

SYSTEM_PROMPT = """\
You are a friendly assistant. Answer briefly.

`get_weather` returns the weather for a city: call it rather than guessing. You may also run
shell commands with Bash when the user asks for something a command can answer."""


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city. `city` is a plain city name, e.g. "Paris"."""
    return f"It's 72°F and sunny in {city}."


@agent.defn(name="ClaudeHelloAgent")
class ClaudeHelloAgentWorkflow:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Safe by default: get_weather is inherently safe and runs; Bash is not, so every
            # command waits for a human `tool_approval` in the UI.
            approval_policy_default=ToolApprovalPolicy(),
        )
        self._claude: HarnessClaudeAgent | None = None

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with Claude. Ask about the weather somewhere, or ask it to run a command
        (it will wait for your approval first)."""
        if self._claude is None:
            # Created on first message: DurableClaudeAgent keeps the conversation across
            # run() calls, so later messages continue the same Claude session.
            self._claude = HarnessClaudeAgent(
                self._runner,
                system_prompt=SYSTEM_PROMPT,
                tools=[get_weather],
                builtin_tools=["Bash"],
            )
        return TextReply(text=await self._claude.run(message.text))
