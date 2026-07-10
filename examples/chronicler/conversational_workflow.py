"""Chronicler: a conversational D&D campaign archivist with Code Mode over audio tools.

You chat in plain text; a *model in the loop* converses, then **writes a Python script and runs
it** to transcribe session audio, summarize it, extract campaign entities, and synthesize spoken
recaps — replying in prose. It does this through the harness Code Mode feature:
:func:`agent.code_mode_tool` turns the durable audio activities into a single
``run_chronicler_code`` tool that runs a model-authored script in a sandbox, where each tool is an
async host function the script calls. Every host call runs as a durable, approval-gated activity,
and the script can combine many with real control flow (loops, ``asyncio.gather`` concurrency) —
e.g. transcribe the last three sessions concurrently, then summarize each, then voice a recap.

The conversational front end is a Gemini Interactions tool-calling loop exposing that one Code
Mode tool (lifted from the Monty example). It uses only a custom *function* tool, which chains
cleanly across turns via ``previous_interaction_id``. The Code Mode tool advertises the exact
host-function signatures + result shapes (from the typed pydantic models) in its own generated
description, so the system prompt only needs to set the persona and point the model at the tool.
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
    from .local_fs_tools import LOCAL_TOOLS


TASK_QUEUE = "chronicler-agent"
SUPPORTED_MODELS = ("gemini-3.1-flash-lite",)
DEFAULT_MODEL = SUPPORTED_MODELS[0]


def model_slash_command(set_model) -> slash_commands.SlashCommandDefinition:
    return slash_commands.model_selector(
        choices=SUPPORTED_MODELS,
        set_model=set_model,
        description="Set the model for this Chronicler session.",
    )


SYSTEM_INSTRUCTION = """\
You are the Chronicler — a warm, evocative archivist for a tabletop RPG (D&D) campaign. You help \
the party remember what happened: you transcribe session recordings, summarize them, track the \
NPCs/locations/quests, produce spoken recaps ("previously, on…") and intros, and build a static \
campaign website.

You WRITE small async Python scripts and run them with the `run_chronicler_code` tool, which \
exposes every operation as an async host function your script calls. The tool's description gives \
the exact signatures and result shapes — follow them and index results with normal Python.

WHERE THINGS LIVE (important): you run on a server with NO storage of its own. All of the user's \
data — recordings, the session registry, transcripts, and the website — lives on THE USER'S OWN \
MACHINE, reached through host functions a small bridge on their laptop fulfills: `list_sessions`, \
`ingest_sessions`, `save_recording`, `upload_recording`, `save_transcript`, `read_transcript`, \
and the site filesystem (`ls`/`tree`/`read_file`/`write_file`/`delete_file`/`grep`/ \
`write_binary_file`). The compute tools (`transcribe_recording`, `summarize_transcript`, \
`extract_entities`, `synthesize_audio`, `generate_sample_audio`) run on the server and return \
data — you move that data to/from the user's machine yourself.

How to behave:
- Converse naturally. Call `list_sessions(None)` (campaign_id = null) first to see every session \
and its campaign — never guess a campaign name. Then WRITE A SCRIPT.
- DISCOVERABILITY: when the user greets you, asks what you can do, or `list_sessions` is empty, \
PROACTIVELY offer to "generate a sample session". To make one: \
`s = await generate_sample_audio(installment)` then \
`await save_recording("duskblade", s.title, s.audio_base64)`. The samples are ONE continuing \
story indexed by `installment` (1-based play order): pass the NEXT number after the sessions that \
already exist. To make several at once, pass consecutive installments (1, 2, 3, …) — never the \
same one twice — so they continue the arc instead of repeating.
- `list_sessions` also reports `unregistered_files` — recordings the user dropped in but hasn't \
registered. Offer to `ingest_sessions(campaign_id)` them, then continue.
- TRANSCRIBE (two steps — the audio stays on the user's machine): \
`ref = await upload_recording(session_id)` (their bridge uploads it to Gemini), then \
`t = await transcribe_recording(ref, session_id)`, then `await save_transcript(t)` so it persists \
on their disk. Transcription is the long step — after it finishes, `notify(...)` them.
- SUMMARIZE / EXTRACT work by session_id: right after transcribing this run, \
`await summarize_transcript(session_id)` / `await extract_entities(session_id)` just work. For a \
session you did NOT transcribe this run, first `t = await read_transcript(session_id)` and pass \
`t.full_text` as the `transcript_text` argument.
- BUILD THE SITE when asked for a "site"/"website": write self-contained HTML under `site/` with \
`write_file` (e.g. `site/index.html` and `site/sessions/<id>.html`) so it opens with no server. \
For audio, `a = await synthesize_audio(request)` then \
`await write_binary_file("site/audio/<name>.wav", a.audio_base64)` and reference it with an \
`<audio>` tag. Orient with `tree`/`ls` and `read_file` before overwriting existing pages.
- Run independent work CONCURRENTLY with `asyncio.gather`; only await sequentially when a later \
call needs an earlier result.
- Keep heavy text out of your reply: work by id, never print full transcripts — give the recap, \
the beats, the cliffhanger, who's who, and where you saved things (the paths).
- Never invent events, NPCs, or quotes — only use what the transcripts and tools return."""


@workflow.defn(name="ChroniclerAgent")
@agent.defn
class ChroniclerAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Demo stance: require human approval for EVERY tool call — the `run_chronicler_code`
            # tool and each host call the script makes (transcribe/summarize/synthesize/notify),
            # since every call is dispatched through run_tool and gated.
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
            slash_commands=[
                *slash_commands.default_commands(),
                model_slash_command(self._set_model),
            ],
        )
        self._model: str = DEFAULT_MODEL
        # Server-side conversation chaining id (Interactions API); updated each turn. Safe to
        # chain here because this agent uses only a function tool (no file_search).
        self._previous_interaction_id: str | None = None
        # The single model-facing tool: Code Mode over the archive tools. The model writes a
        # Python script that calls them as async host functions; each host call runs as a
        # durable, approval-gated activity via run_tool.
        self._code_tool = agent.code_mode_tool(
            [
                # Stateless Gemini compute — run on the worker, touch no disk.
                tools.generate_sample_audio_activity,
                tools.transcribe_recording_activity,
                tools.summarize_transcript_activity,
                tools.extract_entities_activity,
                tools.synthesize_audio_activity,
                tools.notify_activity,
                # Callback tools fulfilled by the bridge on the DM's own machine (see
                # local_fs_tools.py / local_bridge.py): the session registry, recordings ->
                # Gemini upload, transcript persistence, and the static-site filesystem. They
                # compose with the compute tools above because a callback routes through the same
                # run_tool path — approval-gated and dispatchable as a Code Mode host function like
                # any other. This is what keeps the worker stateless: all I/O happens here.
                *LOCAL_TOOLS,
            ],
            name="run_chronicler_code",
        )

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
        """Chat with the Chronicler. Ask for a recap of last session, who the party met, a
        'previously on' audio intro, or to transcribe a new recording; the archivist converses,
        writes and runs Python scripts against the durable audio tools, and replies with the
        results."""
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
        """Run one conversational turn: stream the model, dispatch any ``run_chronicler_code``
        calls, feed results back, and loop until the model replies with no further calls.

        Updates ``self._previous_interaction_id`` for chaining the next turn (no file_search
        here, so chaining is safe)."""
        tools = [function_param(self._code_tool)]
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
        """Execute one ``run_chronicler_code`` call via ``run_tool`` and return its result.

        ``run_tool`` parks the call id so the script's host calls and the tool's own
        lifecycle events correlate with the streaming activity's ``tool_requested``."""
        try:
            if call.name != self._code_tool.__name__:
                raise ValueError(f"unknown tool: {call.name!r}")
            result = await self._runner.run_tool(
                call.id, self._code_tool, **call.arguments
            )
            response: FunctionResultStepParam = {
                "type": "function_result",
                "call_id": call.id,
                "name": call.name,
                "result": str(result),
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

        Returns ``(reply_text, function_calls, interaction_id)``. Text comes from
        ``DeltaText`` events; function calls are captured from each ``StepStart`` whose step
        is a ``FunctionCallStep``, with their JSON-string ``arguments`` fragments buffered per
        step index and ``json.loads``-ed once the stream ends. (Lifted verbatim from the Monty
        agent's loop.) Raises :class:`ApplicationError` on stream errors or if the stream
        ends without a completed event."""
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
