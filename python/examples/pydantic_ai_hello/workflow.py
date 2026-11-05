"""A hello-world Pydantic AI agent on the harness, with one tool call.

A conversational agent that answers in plain text and can call a single tool (``get_weather``).
The agent is made durable with ``TemporalAgentHarnessDurability``, so each streamed model call routes
through Pydantic AI's model activity, where the harness translates raw ``AgentStreamEvent``s into
the turn-stream events an attached client sees. The tool is a normal
harness tool adapted onto the SDK with ``build_harness_toolset``, so the harness still owns approval
and its ``tool_start`` / ``tool_end`` / ``tool_error`` events.

The agent is built ONCE at module load — its activities are registered on the worker via
``AgentPlugin`` (see worker.py). It carries no runner: the per-turn runner is threaded EXPLICITLY
through ``deps`` at each ``run(...)`` call (``HarnessDeps(runner=...)``, which snapshots the turn's
stream context from it), so one shared agent serves every concurrent workflow correctly — no
``self._runner`` assumption.

Run it with the shared example stack (session-manager worker + FastAPI/UI); this agent is registered
in ``agents.toml`` and driven by the packaged web app. See ``README.md``.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelMessage

    from temporal_agent_harness.ai_sdks.pydantic_ai_harness import (
        HarnessDeps,
        TemporalAgentHarnessDurability,
        build_harness_toolset,
    )
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


TASK_QUEUE = "pydantic-ai-hello"
AGENT_NAME = "pydantic-ai-hello"
DEFAULT_MODEL = "openai:gpt-5.1"
TOOLSET_ID = "hello-tools"

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


# Built once at module load: harness tools adapted into a Pydantic AI FunctionToolset whose tools
# run in-workflow (where the harness approval gate + tool events live, and the runner is on deps).
_TOOLSET = build_harness_toolset([get_weather], id=TOOLSET_ID)

# The durable agent. Its activities (model_request(_stream) and event_stream_handler) are registered
# on the worker via AgentPlugin(_AGENT). deps_type=HarnessDeps lets Temporal (de)serialize the
# per-turn stream context that rides on deps into the model activity.
_AGENT = Agent(
    DEFAULT_MODEL,
    name=AGENT_NAME,
    instructions=SYSTEM_INSTRUCTION,
    deps_type=HarnessDeps,
    toolsets=[_TOOLSET],
    capabilities=[TemporalAgentHarnessDurability()],
)


@agent.defn(name="PydanticAIHelloAgent")
class PydanticAIHelloAgentWorkflow:
    """A one-tool conversational agent driven by Pydantic AI."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Hello-world stance: don't gate tool calls. A caller can tighten this per session via
            # AgentConfig.approval_policy.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # Pydantic AI conversation state, threaded across turns as its message history.
        self._history: list[ModelMessage] = []

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant. Ask it anything; ask about the weather in a city and it calls
        its `get_weather` tool and tells you what it found."""
        # Thread the runner EXPLICITLY through deps (not assumed off the workflow instance). Passing
        # just the runner is enough: HarnessDeps snapshots the in-flight turn's stream context from
        # it — the live runner is read in-workflow by the adapted tools, and the snapshotted context
        # rides into the model activity where the streaming handler publishes.
        result = await _AGENT.run(
            message.text,
            deps=HarnessDeps(runner=self._runner),
            message_history=self._history,
        )
        self._history = result.all_messages()
        return TextReply(text=str(result.output))
