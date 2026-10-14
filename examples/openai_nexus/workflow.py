"""A hello-world OpenAI Agents SDK agent whose model calls travel over Nexus.

This is the sibling of ``examples/openai_hello`` — same harness shape (one ``ask``
handler, one ``get_weather`` tool) so it plugs into the shared web UI the same
way — but with one difference that is the whole point: the LLM call does not go
to the provider from a Temporal activity. It goes over a Nexus operation to a
standalone model router (``nexus/model_router``), which is what calls OpenAI.

Why this needs the plugin's workflow-side seam rather than a plain custom model
provider: in the Temporal integration the model call runs inside an activity,
but ``workflow.create_nexus_client`` only works in workflow context. So the model
is resolved and called from the model stub (which does run in the workflow) via
``ModelActivityParameters.workflow_model_provider`` — wired in ``worker.py`` to a
model whose transport is the router Nexus service (see ``nexus_transport.py``).

Streaming is intentionally out of scope, so this uses ``Runner.run`` (blocking),
not ``Runner.run_streamed``. That means there is no live token/model-interaction
stream (the streaming observer only runs on the activity streaming path); the
harness still delivers the final reply and — because ``get_weather`` is adapted
with ``as_openai_agent_tool`` — the ``tool_start`` / ``tool_end`` lifecycle to the
UI. Drive it with the shared example stack; it is registered in ``agents.toml``.
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


# The agent workflow's own task queue. The model transport (Nexus → the model
# router) is wired on the worker, so this workflow stays transport-agnostic.
TASK_QUEUE = "openai-nexus"
DEFAULT_MODEL = "gpt-4.1-mini"

SYSTEM_INSTRUCTION = """\
You are a friendly assistant. Answer the user in brief, natural prose.

You have one tool, `get_weather`, which returns the current weather for a city. When the user
asks about the weather somewhere, call it (don't guess), then tell them the answer in a sentence
or two. For anything else, just reply directly."""


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city. `city` is a plain city name, e.g. "Paris"."""
    # Canned lookup — this is a hello-world, no real weather service — but a genuine harness
    # tool call: adapted onto the SDK with as_openai_agent_tool, it flows through run_tool and
    # shows up on the turn stream as tool_start -> tool_end.
    return f"It's 72°F and sunny in {city}."


@workflow.defn(name="OpenAINexusAgent")
@agent.defn
class OpenAINexusAgentWorkflow:
    """A one-tool conversational agent whose model calls are made over Nexus."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Hello-world stance: don't gate tool calls. `get_weather` is a read-only lookup;
            # a caller can still tighten this per session via AgentConfig.approval_policy.
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
        calls its `get_weather` tool and tells you what it found — with every model call routed
        over Nexus to the LLM service."""
        sdk_agent = OpenAIAgent(
            name="NexusHello",
            instructions=SYSTEM_INSTRUCTION,
            model=DEFAULT_MODEL,
            tools=[as_openai_agent_tool(self._runner, get_weather)],
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]

        # Runner.run (NOT run_streamed): the Nexus model path is non-streaming. Each
        # model.get_response the SDK makes is dispatched from the model stub over Nexus to
        # the model router's chat_completion op (see worker.py / nexus_transport.py) instead
        # of running as an activity. A weather question is typically two such calls: the model
        # asks for the tool, the tool runs in the workflow, then a second Nexus call answers.
        result = await Runner.run(sdk_agent, input=input_items)

        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))
