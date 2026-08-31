"""The ReAct agent: a multi-tool OpenAI Agents SDK agent on the harness.

A conversational agent in the ReAct pattern — it reasons, then acts by calling a tool, and loops
on the result until it can answer in plain text. It finds the weather for a named city
(``get_coordinates`` -> ``get_weather``) or for the user's current location (``get_ip_address``
-> ``get_location_info`` -> ``get_weather``), and pulls Formula 1 data through an MCP server.

Streaming is a toggle (``REACT_AGENT_STREAM``, default on — see ``STREAM_RESPONSES``). Streaming
runs the model with ``Runner.run_streamed(..., context=self._runner)`` — passing the harness runner
as the SDK run context is what lets the streaming seam resolve the in-flight turn — so model calls
route through the streaming activity and the harness observer translates raw OpenAI events into the
live turn stream. Non-streaming uses ``Runner.run(...)``: the turn runs to completion and returns
one reply, with tool lifecycle / ``ask_user`` / the final reply still on the turn stream but no
``reply_delta`` or ``model_interaction_*``. The
local tools are durable harness activity tools adapted onto the SDK with ``as_openai_agent_tools``
(so the harness owns the approval policy and each tool's ``tool_start`` / ``tool_end`` /
``tool_error`` events); the F1 tools come from a durable, activity-backed MCP server registered on
the worker and referenced here with ``stateless_mcp_server``. It also has one human-in-the-loop
tool, ``ask_user`` (an ``@agent.callback_tool_defn`` callback tool): when the model needs
clarification it parks the turn in-workflow until an external client returns the user's answer —
see ``client.py``.

This is the harness form of workshop demo2/demo3 (OpenAI Agents SDK + Temporal, then MCP): the
Agents SDK drives the reason-act loop; Temporal makes it durable; the harness standardizes it.
Registered in ``agents.toml`` and driven by the shared example stack (session-manager worker +
FastAPI/UI). See ``README.md``.
"""

from __future__ import annotations

import os

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

    from .human_tools import HUMAN_TOOLS
    from .tool_activities import ALL_TOOLS


TASK_QUEUE = "react-agent"
DEFAULT_MODEL = "gpt-5.6-luna"
MCP_SERVER_NAME = "f1-data"

# Streaming vs non-streaming is chosen here, at the SDK call site (see `ask`). Toggle it with the
# REACT_AGENT_STREAM env var in the WORKER's environment (the workflow runs on the worker). Default
# is streaming — today's behavior. Read once at import, so it's fixed per worker process, not per
# turn; keep it consistent across worker restarts for a given session (it isn't recorded in
# workflow history — see README).
STREAM_RESPONSES = os.environ.get("REACT_AGENT_STREAM", "1").lower() not in {
    "0",
    "false",
    "no",
}

SYSTEM_INSTRUCTION = """\
You are a helpful location and weather assistant. Answer the user in brief, natural prose.

Use your tools to answer accurately rather than guessing. You can find the weather two ways:
 - For a named city: look up its coordinates with `get_coordinates`, then call `get_weather`.
 - For the user's current location: call `get_ip_address`, then `get_location_info` (to get
   coordinates from the IP), then `get_weather`.
Chain tools as needed — you will usually need more than one — and once you have enough
information, reply in a sentence or two. `get_weather` returns the temperature in Fahrenheit, a
weather code, and wind speed; summarize it in plain language.

If a request is ambiguous or needs information only the user can give — which city they mean, or
permission to use their current location — call `ask_user` with a clear question and use their
answer to continue. Prefer asking over guessing when it matters. If the user's answer is not clear
call `ask_user` again."""


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
            # ALL_TOOLS are durable activity tools; HUMAN_TOOLS is the ask_user callback tool.
            # Both mix freely through the one adapter — a callback tool carries __agent_tool__ and
            # funnels into run_tool exactly like an activity tool.
            tools=as_openai_agent_tools(self._runner, [*ALL_TOOLS, *HUMAN_TOOLS]),
            # Reference the worker-registered MCP provider by name; stateless_mcp_server
            # returns the durable reference the runner resolves to activity-backed MCP
            # operations. Passing the bare name string here is silently non-durable.
            # cache_tools_list=True: the SDK re-gathers tools on every inner-loop step, so without
            # caching each step re-runs the MCP `list_tools` activity. The F1 tool set is static, so
            # cache it — one `list_tools` per turn instead of one per model step. (The reference is
            # rebuilt each turn, so the cache is per-turn, not per-session.)
            mcp_servers=[stateless_mcp_server(MCP_SERVER_NAME, cache_tools_list=True)],
        )
        input_items: list[TResponseInputItem] = [
            *self._conversation,
            {"role": "user", "content": message.text},
        ]

        if STREAM_RESPONSES:
            # run_streamed returns immediately; iterate its events to drive the turn to completion.
            # context=self._runner hands the harness runner to the streaming seam
            # (stream_to_provider reads the in-flight turn off it); required only on this path.
            result = Runner.run_streamed(sdk_agent, input=input_items, context=self._runner)
            async for _event in result.stream_events():
                pass
        else:
            # Non-streaming: run the whole turn to completion, then return one reply. No context
            # and no stream seam. Tool cards, ask_user, and the final reply still appear on the turn
            # stream; token-by-token reply_delta and model_interaction_* do not.
            result = await Runner.run(sdk_agent, input=input_items)

        self._conversation = result.to_input_list()
        return TextReply(text=str(result.final_output))
