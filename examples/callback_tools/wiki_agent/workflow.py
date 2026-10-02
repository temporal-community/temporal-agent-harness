"""A conversational wiki-organizing agent built on harness CALLBACK TOOLS.

The user chats in plain text ("jot this down", "what do I have on X?", "clean up my recipes").
A *model in the loop* converses and decides how to organize a tree of Markdown files — when to
create a new note, append to an existing one, delete an obsolete one, or restructure — by calling
filesystem tools. Those tools are **callback tools**: the agent has no disk of its own (picture it
running in a cloud worker), so each tool call pauses in-workflow and a thin client on the user's
machine (``client.py``) executes it against a local wiki directory and returns the result. The
agent just calls ``read_file`` / ``write_file`` / ``ls`` / ``tree`` / ``delete_file`` / ``grep``
like any tool and reasons over the results.

The conversational front end is a Gemini Interactions tool-calling loop — the same shape as the
Monty conversational agent — but instead of one Code Mode tool it exposes the six callback tools
directly. Each tool's own docstring (in ``tools.py``) is its model-facing description, so the
system prompt only sets the persona and the organizing philosophy.
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

    from .tools import WIKI_TOOLS


TASK_QUEUE = "wiki-agent"
DEFAULT_MODEL = "gemini-3.5-flash"


SYSTEM_INSTRUCTION = """\
You are a meticulous personal wiki keeper. The user talks to you in plain language — telling you \
things to remember, asking what they've noted, or asking you to tidy up — and YOU decide how to \
organize it all as a tree of Markdown files.

You do not have a filesystem yourself. You act on the user's wiki only through these tools, which \
run on the user's machine: `ls`, `tree`, `read_file`, `write_file`, `delete_file`, `grep`. Their \
descriptions give the exact signatures — follow them.

How to behave:
- ORIENT before you write. Use `tree`/`ls`/`grep` to see what already exists so related notes \
stay together, rather than scattering near-duplicates.
- Prefer APPENDING to an existing note over making a new file when the topic already has a home: \
`read_file` it, then `write_file` the full revised contents (write_file overwrites, so include \
everything you want to keep).
- Make a NEW file when the topic is genuinely new; choose a clear, kebab-case path with a sensible \
folder (e.g. "recipes/carbonara.md", "projects/temporal/notes.md"). Every note is Markdown — give \
it a top-level "# Title".
- DELETE only when a note is clearly obsolete or the user asks.
- Keep changes small and purposeful; you may call several tools across turns. Never invent file \
contents you didn't read.
- After acting, reply to the user in brief, friendly prose: say what you recorded and where \
(the path), so they can find it later."""


@agent.defn(name="WikiAgent")
class WikiAgentWorkflow:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # The tools run on the user's own machine — the human attached in the terminal IS the
            # one executing each call — so a separate human-approval gate would be redundant here.
            # Skip approvals by default; a caller can still tighten this per session via
            # AgentConfig.approval_policy (callback tools honor the policy like any other tool).
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._model: str = DEFAULT_MODEL
        # The whole conversation, sent as the input of every model call (see
        # InteractionConversation for why it is not chained by interaction id).
        self._conversation = InteractionConversation()
        # The model-facing callback toolset, plus a name -> tool map for dispatch.
        self._tools = list(WIKI_TOOLS)
        self._tools_by_name = {tool.__name__: tool for tool in self._tools}
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
        """Chat with the wiki keeper. Tell it something to remember, ask what you've noted, or ask
        it to reorganize; it reads and edits your local Markdown wiki through callback tools and
        replies with what it did."""
        reply_text = await self._handle_chat_turn(self._gemini, message.text)
        return TextReply(text=reply_text)

    # ------------------------------------------------------------------ chat loop

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one conversational turn: stream the model, dispatch any tool calls (each pausing
        for the client to fulfill), feed results back, and loop until the model replies with no
        further calls.

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

        For a callback tool, ``run_tool`` dispatches the standard inline-tool path (approval
        policy + tool_start/end) and the tool body parks on ``callback_requested`` until the
        client supplies a result — so this await blocks (durably) until the user's machine
        answers. The result is rendered to text for the model."""
        try:
            tool = self._tools_by_name.get(call.name)
            if tool is None:
                raise ValueError(f"unknown tool: {call.name!r}")
            result = await self._runner.run_tool(call.id, tool, injections=None, **call.arguments)
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
    """Render a tool's return value to text for the model. Callback tools here return ``str`` or
    ``list[str]``; a list is JSON-encoded so the model sees clean structure, a string is passed
    through as-is."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result)
    except TypeError:
        return str(result)
