"""A hello-world OpenAI Agents SDK agent on the harness, with one tool call.

A conversational agent that answers in plain text and can call a single tool
(``get_weather``). The turn runs the model with ``Runner.run_streamed(...)``, which routes
model calls through the streaming activity so the harness observer can translate raw
OpenAI events into the turn-stream events an attached client sees. The tool is a normal
harness tool adapted onto the SDK with ``as_openai_agent_tool``, so the harness still owns
approval and its ``tool_start`` / ``tool_end`` / ``tool_error`` events.

Run it with the shared example stack (session-manager worker + FastAPI/UI); this agent is
registered in ``agents.toml`` and driven by the packaged web app. See ``README.md``.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import Runner, TResponseInputItem

    from temporal_agent_harness.ai_sdks.openai_agents_harness import as_openai_agent_tool
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


TASK_QUEUE = "openai-hello"
DEFAULT_MODEL = "gpt-5.1"

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


@workflow.defn(name="OpenAIHelloAgent")
@agent.defn
class OpenAIHelloAgentWorkflow:
    """A one-tool conversational agent driven by the OpenAI Agents SDK."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Hello-world stance: don't gate tool calls. A caller can tighten this per
            # session via AgentConfig.approval_policy.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # OpenAI conversation state, threaded across turns as the SDK's input-item list.
        self._conversation: list[TResponseInputItem] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant. Ask it anything; ask about the weather in a city and it
        calls its `get_weather` tool and tells you what it found."""
        sdk_agent = OpenAIAgent(
            name="Hello",
            instructions=SYSTEM_INSTRUCTION,
            model=DEFAULT_MODEL,
            tools=[as_openai_agent_tool(self._runner, get_weather)],
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]

        # run_streamed returns immediately; iterate its events to drive the turn to completion.
        # context=self._runner threads the harness runner to the streaming activity so the
        # plugin can route live events to this turn's stream.
        result = Runner.run_streamed(sdk_agent, input=input_items, context=self._runner)
        async for _event in result.stream_events():
            pass

        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))
