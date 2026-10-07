"""A hello-world OpenAI Codex agent on the harness, with one tool call.

A conversational agent whose agent loop is the Codex app-server. It can call a single tool
(``get_weather``), which is a normal harness tool: Codex's own built-in tools are turned off, so
every tool the model calls is a host tool that runs through ``runner.run_tool(...)`` and the harness
still owns approval and its ``tool_start`` / ``tool_end`` / ``tool_error`` events.

Run it with the shared example stack (session-manager worker + FastAPI/UI); this agent is
registered in ``agents.toml`` and driven by the packaged web app. See ``README.md``.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.ai_sdks.codex_harness import CodexSession
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


TASK_QUEUE = "codex-hello"

SYSTEM_INSTRUCTION = """\
You are a friendly assistant. Answer the user in brief, natural prose.

You have one tool, `get_weather`, which returns the current weather for a city. When the user
asks about the weather somewhere, call it (don't guess), then tell them the answer in a sentence
or two. For anything else, just reply directly."""


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city. `city` is a plain city name, e.g. "Paris"."""
    # Canned lookup — a hello-world, not a real weather service.
    return f"It's 72°F and sunny in {city}."


@agent.defn(name="CodexHelloAgent")
class CodexHelloAgentWorkflow:
    """A one-tool conversational agent driven by the Codex agent loop."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Hello-world stance: don't gate tool calls. A caller can tighten this per
            # session via AgentConfig.approval_policy.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # The Codex thread: held here (thread id + rollout), so a Worker crash mid-turn resumes it.
        self._codex = CodexSession(tools=[get_weather], instructions=SYSTEM_INSTRUCTION)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant. Ask it anything; ask about the weather in a city and it
        calls its `get_weather` tool and tells you what it found."""
        result = await self._codex.run(self._runner, message.text)
        return TextReply(text=result.text)
