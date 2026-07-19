"""The ReAct agent: a multi-tool OpenAI Agents SDK agent on the harness.

A conversational agent in the ReAct pattern — it reasons, then acts by calling a tool, and loops
on the result until it can answer in plain text. It finds the weather for a named city
(``get_coordinates`` -> ``get_weather``) or for the user's current location (``get_ip_address``
-> ``get_location_info`` -> ``get_weather``), and pulls Formula 1 data through an MCP server. The
turn runs the model with ``Runner.run_streamed(...)`` so model calls route through the streaming
activity and the harness observer translates raw OpenAI events into the live turn stream. The
local tools are durable harness activity tools adapted onto the SDK with ``as_openai_agent_tools``
(so the harness owns the approval policy and each tool's ``tool_start`` / ``tool_end`` /
``tool_error`` events); the F1 tools come from a durable, activity-backed MCP server registered on
the worker and referenced here with ``stateless_mcp_server``.

This is the harness form of workshop demo2/demo3 (OpenAI Agents SDK + Temporal, then MCP): the
Agents SDK drives the reason-act loop; Temporal makes it durable; the harness standardizes it.
Registered in ``agents.toml`` and driven by the shared example stack (session-manager worker +
FastAPI/UI). See ``README.md``.
"""

from __future__ import annotations

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import Agent as OpenAIAgent
    from agents import Runner, TResponseInputItem

    from temporal_agent_harness.ai_sdks.openai_agents.workflow import stateless_mcp_server
    from temporal_agent_harness.ai_sdks.openai_agents_harness import as_openai_agent_tools
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from .tool_activities import ALL_TOOLS


TASK_QUEUE = "react-agent"
DEFAULT_MODEL = "gpt-5.1"
MCP_SERVER_NAME = "f1-data"

SYSTEM_INSTRUCTION = """\
You are a helpful location and weather assistant. Answer the user in brief, natural prose.

Use your tools to answer accurately rather than guessing. You can find the weather two ways:
 - For a named city: look up its coordinates with `get_coordinates`, then call `get_weather`.
 - For the user's current location: call `get_ip_address`, then `get_location_info` (to get
   coordinates from the IP), then `get_weather`.
Chain tools as needed — you will usually need more than one — and once you have enough
information, reply in a sentence or two. `get_weather` returns the temperature in Fahrenheit, a
weather code, and wind speed; summarize it in plain language."""


@workflow.defn(name="ReactAgent")
@agent.defn
class ReactAgentWorkflow:
    """A ReAct agent (weather/geo/IP tools + F1 MCP) driven by the OpenAI Agents SDK."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # No human-in-the-loop yet — don't gate tool calls (demo4-hitl will tighten this).
            # A caller can still override per session via AgentConfig.approval_policy.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # OpenAI conversation state, threaded across turns as the SDK's input-item list.
        self._conversation: list[TResponseInputItem] = []

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the assistant. Ask about the weather in a city, or where you are, and it
        chains its tools (coordinates / IP-location / weather) and tells you what it found."""
        sdk_agent = OpenAIAgent(
            name="ReactAgent",
            instructions=SYSTEM_INSTRUCTION,
            model=DEFAULT_MODEL,
            tools=as_openai_agent_tools(self._runner, ALL_TOOLS),
            # Reference the worker-registered MCP provider by name; stateless_mcp_server
            # returns the durable reference the runner resolves to activity-backed MCP
            # operations. Passing the bare name string here is silently non-durable.
            mcp_servers=[stateless_mcp_server(MCP_SERVER_NAME)],
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]

        # run_streamed returns immediately; iterate its events to drive the turn to completion.
        result = Runner.run_streamed(sdk_agent, input=input_items)
        async for _event in result.stream_events():
            pass

        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))
