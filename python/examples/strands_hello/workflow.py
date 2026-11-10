"""A hello-world Strands Agents agent on the harness, with every kind of tool it takes.

A conversational agent that answers in plain text. It is a Strands ``Agent`` built with
``harness_strands_agent``, so each model call runs in the Strands plugin's model activity, where
the harness translates raw Strands stream events into the turn-stream events an attached client
sees. Its tools cover each kind a harness Strands agent accepts:

* ``get_weather`` — an inline workflow tool (``@agent.tool_defn``).
* ``get_local_time`` — an activity tool (``@agent.activity_tool_defn``): reading the clock is
  nondeterministic, so it runs on the worker as a durable activity.
* ``ask_user`` — a callback tool (``@agent.callback_tool_defn``): the call waits in the workflow
  until the person in the console answers.
* ``sandbox_shell`` / ``sandbox_file_editor`` — vended by ``TemporalSandbox("scratch")``. Each
  sandbox operation is an activity against the worker's ``scratch`` sandbox (see worker.py).

A message sent while the agent is working steers it: ``ask`` joins the open turn and queues the
text with ``runner.steer``, and the harness hands it to the model with the next tool result.

Run it with the shared example stack (session-manager worker + FastAPI/UI); this agent is registered
in ``agents.toml`` and driven by the packaged web app. See ``README.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.ai_sdks.strands_agents import TemporalSandbox
    from temporal_agent_harness.ai_sdks.strands_agents_harness import (
        as_strands_tool,
        harness_strands_agent,
    )
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        MessageContext,
        MidTurn,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner, Injected


TASK_QUEUE = "strands-hello"
# The name StrandsPlugin(models=...) registers the worker's model under (see worker.py).
MODEL = "openai"
# The name StrandsPlugin(sandboxes=...) registers the worker's sandbox under (see worker.py).
SANDBOX = "scratch"

SYSTEM_PROMPT = """\
You are a friendly assistant. Answer the user in brief, natural prose.

Your tools:
- `get_weather`: the current weather for a city. Call it rather than guess.
- `get_local_time`: the current local time in an IANA time zone, e.g. "Europe/Paris".
- `ask_user`: ask the user a question when you need something only they know.
- `sandbox_shell` and `sandbox_file_editor`: run shell commands and edit files in a scratch
  workspace. Use them when the user asks you to run, write, or inspect something there."""


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city. `city` is a plain city name, e.g. "Paris"."""
    # Canned lookup — a hello-world, not a real weather service.
    return f"It's 72°F and sunny in {city}."


@agent.activity_tool_defn(inherently_safe=True)
async def get_local_time(time_zone: str) -> str:
    """Return the current local time in an IANA time zone, e.g. "Europe/Paris"."""
    return datetime.now(ZoneInfo(time_zone)).strftime("%A %H:%M (%Z)")


@agent.callback_tool_defn(inherently_safe=True)
async def ask_user(question: str) -> str:
    """Ask the user a question and return their answer."""
    ...


@agent.defn(name="StrandsHelloAgent")
class StrandsHelloAgentWorkflow:
    """A conversational agent driven by Strands Agents, with every kind of harness Strands tool."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # The weather, time and ask-the-user tools are inherently safe and run freely. The
            # sandbox tools are not, so each of their calls waits for a person's approval.
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        # Strands keeps the conversation in the agent, which lives as long as the workflow.
        self._agent = harness_strands_agent(
            self._runner,
            model=MODEL,
            start_to_close_timeout=timedelta(minutes=2),
            system_prompt=SYSTEM_PROMPT,
            tools=[
                as_strands_tool(self._runner, get_weather),
                as_strands_tool(self._runner, get_local_time),
                as_strands_tool(self._runner, ask_user),
            ],
            sandbox=TemporalSandbox(SANDBOX, start_to_close_timeout=timedelta(minutes=2)),
            # Replies reach the console through the turn stream, not Strands' stdout printer.
            callback_handler=None,
        )

    @agent.accepts(mid_turn=MidTurn.ACCEPT)
    async def ask(self, message: TextMessage, ctx: Injected[MessageContext]) -> TextReply:
        """Chat with the assistant. Ask about the weather or the time somewhere, or ask it to run
        something in its scratch workspace; it may ask you a question back. Sent while it is
        working, the message steers it instead: the text reaches the model with the next tool
        result."""
        if ctx.joined_turn:
            self._runner.steer(message.text)
            return TextReply(text="")
        result = await self._agent.invoke_async(message.text)
        return TextReply(text=str(result))
