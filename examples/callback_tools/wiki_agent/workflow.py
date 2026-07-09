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
from functools import partial
from typing import Sequence

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from google.genai._interactions.types import (
        ErrorEvent,
        FunctionCallStep,
        InteractionCompletedEvent,
        StepDelta,
        StepStart,
        ToolParam,
    )
    from google.genai._interactions.types.error_event import Error
    from google.genai._interactions.types.function_result_step_param import (
        FunctionResultStepParam,
    )
    from google.genai._interactions.types.interaction_create_params import Input
    from google.genai._interactions.types.step_delta import (
        DeltaArgumentsDelta,
        DeltaText,
    )
    from google.genai.client import AsyncClient
    from temporal_agent_harness.ai_sdks.google_genai_plugin import (
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


@workflow.defn(name="WikiAgent")
@agent.defn
class WikiAgentWorkflow:
    @workflow.init
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
        # Server-side conversation chaining id (Interactions API); updated each turn. Safe to
        # chain here because this agent uses only function tools (no file_search).
        self._previous_interaction_id: str | None = None
        # The model-facing callback toolset, plus a name -> tool map for dispatch.
        self._tools = list(WIKI_TOOLS)
        self._tools_by_name = {tool.__name__: tool for tool in self._tools}

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        # The Temporal-aware AsyncClient from the Gemini plugin; the runner is wired in so reply
        # text streams to the workflow stream as it is generated.
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(
                start_to_close_timeout=timedelta(minutes=3),
            ),
            runner=self._runner,
        )
        await self._runner.run(self)

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
        further calls. Updates ``self._previous_interaction_id`` for chaining the next turn."""
        tools = [function_param(tool) for tool in self._tools]
        next_input: Input = user_text
        while True:
            (
                reply_text,
                pending_calls,
                self._previous_interaction_id,
            ) = await self._execute_agent_interaction(
                gemini=gemini,
                model=self._model,
                input=next_input,
                tools=tools,
                system_instruction=SYSTEM_INSTRUCTION,
                previous_interaction_id=self._previous_interaction_id,
            )

            if not pending_calls:
                return reply_text

            next_input = await asyncio.gather(
                *(self._run_one_tool(fc) for fc in pending_calls)
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
            result = await self._runner.run_tool(call.id, tool, **call.arguments)
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

    async def _execute_agent_interaction(
        self,
        *,
        gemini: AsyncClient,
        model: str,
        input: Input,
        tools: Sequence[ToolParam],
        system_instruction: str,
        previous_interaction_id: str | None,
    ) -> tuple[str, list[FunctionCallStep], str]:
        """Stream one ``interactions.create`` and reduce it into actionable state.

        Returns ``(reply_text, function_calls, interaction_id)``. Text comes from ``DeltaText``
        events; function calls are captured from each ``StepStart`` whose step is a
        ``FunctionCallStep``, with their JSON-string ``arguments`` fragments buffered per step
        index and ``json.loads``-ed once the stream ends. Raises :class:`ApplicationError` on
        stream errors or if the stream ends without a completed event."""
        interactions_create_fn = partial(
            gemini.interactions.create,
            model=model,
            input=input,
            system_instruction=system_instruction,
            tools=tools,
            stream=True,
        )
        if previous_interaction_id:
            stream = await interactions_create_fn(
                previous_interaction_id=previous_interaction_id
            )
        else:
            stream = await interactions_create_fn()

        text_parts: list[str] = []
        calls_by_index: dict[int, FunctionCallStep] = {}
        arg_buffers: dict[int, str] = {}
        interaction_id: str | None = None
        async for event in stream:
            match event:
                case ErrorEvent(error=Error(message=msg, code=code)):
                    raise ApplicationError(
                        msg or "stream error", type=code or "stream_error"
                    )
                case ErrorEvent():
                    raise ApplicationError("unknown stream error", type="stream_error")
                case StepStart(index=idx, step=FunctionCallStep() as call):
                    calls_by_index[idx] = call
                case StepDelta(
                    index=idx, delta=DeltaArgumentsDelta(arguments=args)
                ) if args:
                    arg_buffers[idx] = arg_buffers.get(idx, "") + args
                case StepDelta(delta=DeltaText(text=text)) if text:
                    text_parts.append(text)
                case InteractionCompletedEvent(interaction=interaction):
                    interaction_id = interaction.id

        if interaction_id is None:
            raise ApplicationError(
                "stream ended without interaction.completed event",
                type="stream_error",
            )

        function_calls = [
            calls_by_index[idx].model_copy(
                update={"arguments": json.loads(arg_buffers[idx])}
            )
            if arg_buffers.get(idx)
            else calls_by_index[idx]
            for idx in sorted(calls_by_index)
        ]
        return "".join(text_parts), function_calls, interaction_id


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
