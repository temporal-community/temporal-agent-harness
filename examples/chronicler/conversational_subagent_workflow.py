"""Chronicler conductor that fans out one SessionScribe SUBAGENT per session (map-reduce).

The subagent-flavoured twin of :class:`ChroniclerAgentWorkflow` (``conversational_workflow.py``).
Both put a *model in the loop* over the same durable audio tools. The difference is HOW the
per-session work runs:

  * The inline agent gives the model **Code Mode** over the tools — one workflow, one context,
    ``asyncio.gather`` concurrency within a single script.
  * This conductor delegates each session to a dedicated :class:`ChroniclerScribeAgent`
    **subagent** — wired via ``agent.subagent_toolset(ChroniclerScribeAgentWorkflow, key="scribe",
    task_queue=TASK_QUEUE)``, which yields ``start_scribe`` / ``scribe_process`` /
    ``scribe_answer`` / ``stop_scribe``. It starts one Scribe per session and drives them
    concurrently (**map**), then combines their typed digests into a campaign-wide recap
    (**reduce**). Each session's processing is its own durable, resumable child workflow.

Alongside the subagent tools, the conductor keeps a few direct tools it needs to discover work
and emit the reduced output: ``list_sessions`` / ``ingest_sessions`` (find sessions),
``synthesize_audio`` (voice the recap), ``notify`` (ping when long transcriptions finish).

Approval stance: ``always_require_approvals`` — the conductor gates every subagent + direct tool
call. Each Scribe runs its internal transcribe/summarize/extract under its own
``dangerously_skip_all`` policy, so those child-internal calls are not re-gated here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import timedelta
from functools import partial
from typing import Any, cast

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
    from pydantic import BaseModel

    from temporal_agent_harness.ai_sdks.google_genai_plugin import function_param, google_genai_client
    from temporal_agent_harness.harness import agent, slash_commands
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        SlashCommand,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from . import chronicler_activities as tools
    from .conversational_workflow import SUPPORTED_MODELS, TASK_QUEUE, model_slash_command
    from .local_fs_tools import (
        ingest_sessions,
        list_sessions,
        save_recording,
        upload_recording,
        write_binary_file,
    )
    from .scribe_workflow import ChroniclerScribeAgentWorkflow


DEFAULT_MODEL = SUPPORTED_MODELS[0]

# Namespace for the wired SessionScribe subagent. Tool names derive from it:
# start_scribe / scribe_process / scribe_answer / stop_scribe.
SUBAGENT_KEY = "scribe"


SYSTEM_INSTRUCTION = f"""\
You are the Chronicler — a warm campaign archivist. You build cross-session recaps and answer \
lore questions by delegating each session to a dedicated SessionScribe subagent, then combining \
what they find.

WHERE THINGS LIVE: you run on a server with NO storage of its own. The user's recordings and \
session registry live on THEIR machine, reached via callback tools a bridge on their laptop \
fulfills (`list_sessions`, `ingest_sessions`, `save_recording`, `upload_recording`, \
`write_binary_file`). The scribes and `synthesize_audio` run on the server and return data.

Tools:
- `list_sessions` (campaign_id, nullable): discover sessions and any `unregistered_files`. Call \
it with campaign_id = null first to see every session and its campaign — never guess a name.
- `generate_sample_audio` (installment) → sample bytes; then `save_recording` (campaign_id, \
title, audio_base64) to store it on the user's machine. Samples are ONE continuing story indexed \
by `installment` (1-based play order) — pass the next number after existing sessions, and use \
consecutive installments (1, 2, 3, …) for several at once so they continue the arc, never repeat. \
DISCOVERABILITY: when the user greets you, asks what you can do, or `list_sessions` is empty, \
PROACTIVELY offer to generate a sample so they have something to try the pipeline on.
- `ingest_sessions` (campaign_id): register recordings the user dropped in.
- `upload_recording` (session_id) → a `file_ref`: uploads that session's local recording to \
Gemini (on the user's machine). Do this BEFORE handing a session to a scribe.
- `start_{SUBAGENT_KEY}`: launch a SessionScribe; returns a short `subagent` handle. Start ONE \
per session you want processed.
- `{SUBAGENT_KEY}_process` (subagent, message={{session_id, file_ref}}): have that scribe \
transcribe (from `file_ref`), summarize, and extract entities; returns a typed digest.
- `{SUBAGENT_KEY}_answer` (subagent, message={{session_id, question}}): ask a scribe about its \
already-processed session.
- `synthesize_audio` (request={{kind, script_text, voice}}) → audio bytes; save them onto the \
user's machine with `write_binary_file` (path, content_base64) if they want the clip.
- `notify` (request={{title, message}}): ping the user (e.g. when long transcriptions finish).

How to behave:
- MAP: to recap or analyze sessions, `list_sessions`, then for EACH target session: \
`upload_recording(session_id)` to get its `file_ref`, `start_{SUBAGENT_KEY}`, and \
`{SUBAGENT_KEY}_process` with message={{session_id, file_ref}}. Issue the per-session work \
CONCURRENTLY (several tool calls in one turn). Track which handle + file_ref belong to which session.
- Transcription is long; after processing finishes, `notify` the user it's ready.
- REDUCE: combine the per-session digests into one campaign-wide result — a chronological \
"previously on", merged and de-duplicated NPC/location/quest lists. Then `synthesize_audio` a \
spoken recap when asked (and `write_binary_file` it to their machine).
- Use `{SUBAGENT_KEY}_answer` for targeted questions about a specific session.
- Never invent events, NPCs, or quotes — only use what the scribes return."""


@workflow.defn(name="ChroniclerSubagentAgent")
@agent.defn
class ChroniclerSubagentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Gate every subagent + direct tool call. Each Scribe's internal transcribe/summarize
            # /extract runs inside the child under its own dangerously_skip_all policy, so those
            # are not re-gated here.
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
            slash_commands=[
                *slash_commands.default_commands(),
                model_slash_command(self._set_model),
            ],
        )
        self._model: str = DEFAULT_MODEL
        # Server-side conversation chaining id (Interactions API); updated each turn. Safe to
        # chain here because this agent uses only function tools (no file_search).
        self._previous_interaction_id: str | None = None
        # Model-facing tools: the SessionScribe subagent toolset (start_scribe / scribe_process /
        # scribe_answer / stop_scribe), built statically from the child's @agent.accepts handlers
        # — no child started here — PLUS the conductor's own direct tools for discovery and
        # emitting the reduced output.
        self._tools = [
            *agent.subagent_toolset(
                ChroniclerScribeAgentWorkflow,
                key=SUBAGENT_KEY,
                task_queue=TASK_QUEUE,
            ),
            # Stateless Gemini compute (server-side).
            tools.generate_sample_audio_activity,
            tools.synthesize_audio_activity,
            tools.notify_activity,
            # Callbacks fulfilled by the bridge on the DM's machine (the worker has no disk): the
            # registry, saving a sample recording, uploading a recording to Gemini for a scribe to
            # transcribe from, and writing synthesized audio onto their machine.
            list_sessions,
            ingest_sessions,
            save_recording,
            upload_recording,
            write_binary_file,
        ]
        self._callables_by_name = {fn.__name__: fn for fn in self._tools}

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        self._gemini = google_genai_client(
            activity_config=ActivityConfig(
                start_to_close_timeout=timedelta(minutes=3),
            ),
            runner=self._runner,
        )
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Chat with the Chronicler conductor. Ask for a cross-session recap, a 'previously on'
        audio intro, or a lore question; it fans out a SessionScribe subagent per session, then
        combines their findings and replies."""
        reply_text = await self._handle_chat_turn(self._gemini, message.text)
        return TextReply(text=reply_text)

    @agent.accepts
    async def slash(self, command: SlashCommand) -> TextReply:
        """Apply a slash command to this parent agent session."""
        return TextReply(
            text=(
                f"Unknown Chronicler slash command: `{command.name}`. Try `/model`. "
                "Harness commands include `/approvals`, `/allow-tools`, and `/status`."
            )
        )

    def _set_model(self, model: str) -> None:
        self._model = model

    # ------------------------------------------------------------------ chat loop

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one conversational turn: stream the model, dispatch any tool calls, feed results
        back, and loop until the model replies with no further calls."""
        tools = [
            function_param(fn)
            for fn in self._tools
            if fn.__name__ != f"stop_{SUBAGENT_KEY}"
        ]
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

        Dispatches through ``runner.run_tool``, which parks this call's id AND the live runner in
        ``_CURRENT_RUNNER`` — exactly how the generated subagent tools reach the runner's subagent
        registry + start/stop/run-turn methods. Results are typed pydantic models (SessionDigest,
        ScribeAnswer, SessionList, AudioArtifact, …), so serialize with ``model_dump_json()`` for
        the model rather than a Python repr."""
        tool_callable = self._callables_by_name.get(call.name)
        try:
            if tool_callable is None:
                raise ValueError(f"unknown tool: {call.name!r}")
            arguments = (
                cast("dict[str, Any]", call.arguments)
                if isinstance(call.arguments, dict)
                else {}
            )
            result = await self._runner.run_tool(call.id, tool_callable, **arguments)
            response: FunctionResultStepParam = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": result.model_dump_json()
                if isinstance(result, BaseModel)
                else str(result),
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
        """Stream one ``interactions.create`` and reduce it into actionable state. (Lifted
        verbatim from the inline conversational agent's loop.)"""
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
