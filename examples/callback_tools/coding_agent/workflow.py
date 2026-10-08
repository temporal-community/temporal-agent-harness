"""A conversational CODING agent built on harness CALLBACK TOOLS.

The user chats in plain text ("add a test for X", "why does this crash?", "refactor this module")
and a *model in the loop* works on their project by calling shell + filesystem tools. Those tools
are **callback tools**: the agent has no disk of its own (picture it running in a cloud worker),
so each tool call pauses in-workflow and a client on the user's machine executes it against the
local project directory and returns the result. Here that client is the **OpenCode shim** — the
same process that fronts the stock OpenCode TUI — so the user chats in OpenCode while a durable
Temporal workflow does the reasoning, and the shim runs the agent's `bash`/`read`/`write`/`edit`
calls on the laptop.

Every tool that touches the user's machine is GATED on their approval (``allow_inherently_safe``):
the mutating tools (``bash``/``write``/``edit``) turn into an OpenCode permission prompt and only
run once the user says yes. Read-only tools (``read``/``grep``/``glob``) and the plan tools
(``todowrite``/``todoread``) are declared ``inherently_safe``, so they auto-approve and can run
concurrently — the "orient" phase isn't throttled by one prompt at a time.

The conversational front end is a Gemini Interactions tool-calling loop — the same shape as the
wiki callback-tools agent — exposing the six coding tools directly. Each tool's own docstring (in
``tools.py``) is its model-facing description, so the system prompt only sets the persona and the
working philosophy.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from google.genai._interactions.types import (
        FunctionCallStep,
    )
    from google.genai._interactions.types.function_result_step_param import (
        FunctionResultStepParam,
    )
    from google.genai.client import AsyncClient
    from temporal_agent_harness.ai_sdks.google_genai_plugin import (
        InteractionConversation,
        function_param,
        google_genai_client,
    )
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from .tools import CODING_TOOLS


TASK_QUEUE = "coding-agent"
DEFAULT_MODEL = "gemini-3.5-flash"


SYSTEM_INSTRUCTION = """\
You are a capable, careful coding assistant working inside the user's project. The user talks to \
you in plain language — asking you to explain code, fix a bug, add a feature, write tests, or run \
a command — and YOU do the work by calling tools.

You do not have a filesystem or shell of your own. You act on the project only through these \
tools, which run on the user's machine: `bash`, `read`, `write`, `edit`, `grep`, `glob`. You also \
have `todowrite`/`todoread` to keep a task list. Their descriptions give the exact signatures — \
follow them. All paths are relative to the project root.

How to work:
- PLAN multi-step work with `todowrite`: lay out the tasks up front, mark one `in_progress`, and \
mark it `completed` as you finish — so the user can follow along. `todoread` recalls the current \
list (it persists across messages). Skip planning for trivial one-step requests.
- ORIENT before you change anything. Use `glob`/`grep`/`read` (or `bash` with `ls`/`cat`) to \
understand the code and conventions before editing, so your changes fit in.
- READ before you EDIT. `edit` needs an exact, unique `old_string`; read the file first. Use \
`edit` for surgical changes and `write` only for new files or full rewrites.
- Every tool call requires the user's approval before it runs — so keep calls purposeful, and \
when a `bash` command is destructive or slow, say so in your reply so the user can decide.
- VERIFY your work when it's cheap to: run the relevant test or build via `bash` after a change.
- Keep going across multiple tool calls until the task is done, then reply in brief, friendly \
prose: what you changed, which files, and how you checked it. Never invent file contents or \
command output you didn't actually read."""


@agent.defn(name="CodingAgent")
class CodingAgentWorkflow:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # This agent runs real shell commands and edits real files on the user's machine, so
            # every call that touches the machine is gated — the shim turns each gated call into an
            # OpenCode permission prompt. Only tools declared `inherently_safe` are auto-approved;
            # here that's just `todowrite` (it edits the plan, not the machine). A caller can still
            # override this per session via AgentConfig.approval_policy.
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._model: str = DEFAULT_MODEL
        # The whole conversation, sent as the input of every model call (see
        # InteractionConversation for why it is not chained by interaction id).
        self._conversation = InteractionConversation()
        # The model-facing toolset, plus a name -> tool map for dispatch.
        self._tools = list(CODING_TOOLS)
        self._tools_by_name = {tool.__name__: tool for tool in self._tools}
        # Durable workflow state: the agent's task list, replaced in place by the inline
        # `todowrite` tool (injected as its `sink`). Survives across turns like any workflow field.
        self._todos: list = []
        # The Temporal-aware AsyncClient from the Gemini plugin; the runner is wired in so reply
        # text streams to the workflow stream as it is generated.
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(
                start_to_close_timeout=timedelta(minutes=3),
            ),
            runner=self._runner,
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the coding agent. Ask it to explain, fix, refactor, test, or run something; it
        reads and edits your local project through callback tools (each gated on your approval) and
        replies with what it did."""
        reply_text = await self._handle_chat_turn(self._gemini, message.text)
        return TextReply(text=reply_text)

    # ------------------------------------------------------------------ chat loop

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one conversational turn: stream the model, dispatch any tool calls (each pausing
        for approval + the shim to fulfill), feed results back, and loop until the model replies
        with no further calls.

        The whole conversation goes out as the input of every call; see
        ``InteractionConversation`` for why it is not chained by interaction id."""
        tools = [function_param(tool) for tool in self._tools]
        self._conversation.add_user_text(user_text)
        while True:
            stream = await gemini.interactions.create(
                model=self._model,
                input=self._conversation.steps,
                system_instruction=SYSTEM_INSTRUCTION,
                tools=tools,
                stream=True,
            )
            reply = await self._conversation.read_reply(stream)
            if not reply.function_calls:
                return reply.text
            self._conversation.add_function_results(
                await asyncio.gather(*(self._run_one_tool(fc) for fc in reply.function_calls))
            )

    async def _run_one_tool(self, call: FunctionCallStep) -> FunctionResultStepParam:
        """Execute one tool call via ``run_tool`` and return its result.

        For a callback tool, ``run_tool`` applies the approval policy + tool_start/end and the tool
        body parks on ``callback_requested`` until the client supplies a result — so this await
        blocks (durably) until the user approves and the shim answers. ``todowrite``/``todoread``
        instead run inline in the workflow; their ``sink`` injection is this agent's own
        ``self._todos`` list, so they edit/read durable workflow state. The result is rendered to
        text for the model."""
        try:
            tool = self._tools_by_name.get(call.name)
            if tool is None:
                raise ValueError(f"unknown tool: {call.name!r}")
            injections = (
                {"sink": self._todos} if call.name in ("todowrite", "todoread") else None
            )
            result = await self._runner.run_tool(
                call.id, tool, injections=injections, **call.arguments
            )
            response: FunctionResultStepParam = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": _render_tool_result(result),
            }
            if call.signature:
                response["signature"] = call.signature
            return response
        except Exception as e:
            response = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": str(e),
                "is_error": True,
            }
            if call.signature:
                response["signature"] = call.signature
            return response


def _render_tool_result(result: object) -> str:
    """Render a tool's return value to text for the model. Callback tools here return ``str``; a
    non-string (shouldn't happen) is JSON-encoded so the model still sees clean structure."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result)
    except TypeError:
        return str(result)
