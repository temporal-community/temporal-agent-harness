"""Conversational parent and review authority for Chronicler audio generation."""

from __future__ import annotations

import hashlib
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
    from google.genai._interactions.types.interaction_create_params import Input
    from google.genai._interactions.types.step_delta import (
        DeltaArgumentsDelta,
        DeltaText,
    )
    from google.genai.client import AsyncClient
    from pydantic import BaseModel, ConfigDict, field_validator
    from temporal_agent_harness.ai_sdks.google_genai_plugin import google_genai_client
    from temporal_agent_harness.harness import agent, slash_commands
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        SlashCommand,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from .audio_models import (
        AudioApprovalPackage,
        AudioDraft,
        AudioDraftResponse,
        AudioGenerationRequest,
        AudioGenerationResult,
        ExistingTranscriptSource,
        PrepareAudioRequest,
        RecoverAudioRequest,
        StartAudioRequest,
    )
    from .audio_tool import generate_audio, launch_audio_child


TASK_QUEUE = "chronicler-agent"
SUPPORTED_MODELS = ("gemini-3.1-flash-lite",)
DEFAULT_MODEL = SUPPORTED_MODELS[0]


def _audio_base_id(
    *,
    workflow_id: str,
    source_kind: str,
    source_identity: str | None,
    source_content: str,
    source_hash: str,
    bridge_id: str,
    root_id: str,
    folder_binding_id: str,
) -> str:
    fields = {
        "bridge_id": bridge_id,
        "folder_binding_id": folder_binding_id,
        "root_id": root_id,
        "source_content": source_content,
        "source_hash": source_hash,
        "source_identity": source_identity,
        "source_kind": source_kind,
        "workflow_id": workflow_id,
    }
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]


def _audio_review_id(
    *,
    workflow_id: str,
    source_kind: str,
    source_identity: str,
    source_content: str,
    source_hash: str,
    recap_script: str,
    wav_path: str,
    synthetic_markdown_path: str | None,
    bridge_id: str,
    root_id: str,
    folder_binding_id: str,
) -> str:
    fields = {
        "bridge_id": bridge_id,
        "folder_binding_id": folder_binding_id,
        "recap_script": recap_script,
        "root_id": root_id,
        "source_content": source_content,
        "source_hash": source_hash,
        "source_identity": source_identity,
        "source_kind": source_kind,
        "synthetic_markdown_path": synthetic_markdown_path,
        "voice": "Charon",
        "wav_path": wav_path,
        "workflow_id": workflow_id,
    }
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:20]


def model_slash_command(set_model) -> slash_commands.SlashCommandDefinition:
    return slash_commands.model_selector(
        choices=SUPPORTED_MODELS,
        set_model=set_model,
        description="Set the model for this Chronicler session.",
    )


SYSTEM_INSTRUCTION = """\
You are Chronicler, a concise guide for creating spoken D&D recaps. Explain that the audio \
workspace lets the user select or draft a transcript, review and revise the exact narration and \
destinations, then explicitly approve generation with the fixed Charon voice. Do not claim to \
have tools, read files, generate audio, or change a review package from chat."""


class _SyntheticAudioPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_content: str
    recap_script: str

    @field_validator("source_content")
    @classmethod
    def validate_source_content(cls, value: str) -> str:
        if not value.startswith("# Synthetic Transcript\n"):
            raise ValueError(
                "synthetic source_content must begin with '# Synthetic Transcript\\n'"
            )
        return value

    @field_validator("recap_script")
    @classmethod
    def validate_recap_script(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("recap_script must be a non-empty string")
        return value


class _AudioDraftRepreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_content: str
    recap_script: str
    wav_path: str
    synthetic_markdown_path: str | None

    @field_validator("source_content", "recap_script", "wav_path")
    @classmethod
    def validate_nonempty_fields(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("audio draft fields must be non-empty strings")
        return value


@workflow.defn(name="ChroniclerAgent")
@agent.defn
class ChroniclerAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
            slash_commands=[
                *slash_commands.default_commands(),
                model_slash_command(self._set_model),
            ],
        )
        self._model: str = DEFAULT_MODEL
        # Server-side conversation chaining id for the tool-free ask surface.
        self._previous_interaction_id: str | None = None
        self._audio_draft: AudioDraft | None = None
        self._audio_package: AudioApprovalPackage | None = None

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
        """Explain the review-first audio workflow without exposing generation tools."""
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

    @agent.accepts
    async def prepare_audio(self, request: PrepareAudioRequest) -> AudioDraftResponse:
        """Use the parent model to create and store a reviewable draft without generating."""
        if request.change_request is not None:
            previous = self._audio_draft
            if previous is None:
                raise ValueError("no prepared audio draft to revise")
            if request.base_draft_digest != previous.draft_digest:
                raise ValueError("base draft does not match the stored audio draft")
            if (
                request.bridge_id,
                request.root_id,
                request.folder_binding_id,
            ) != (
                previous.bridge_id,
                previous.root_id,
                previous.folder_binding_id,
            ):
                raise ValueError("change request binding does not match stored audio draft")
            (
                source_content,
                recap_script,
                wav_path,
                synthetic_markdown_path,
            ) = await self._reprepare_audio_with_model(request, previous)
            source_kind = (
                request.source.source_kind
                if request.source is not None
                else previous.source_kind
            )
            source_hash = previous.source_hash
            source_identity = previous.source_identity
            if isinstance(request.source, ExistingTranscriptSource):
                source_kind = "existing"
                source_hash = request.source.source_hash
                source_identity = request.source.source_identity
            elif request.source is not None:
                source_kind = "synthetic"
                source_hash = hashlib.sha256(source_content.encode()).hexdigest()
            elif previous.source_kind == "synthetic":
                source_hash = hashlib.sha256(source_content.encode()).hexdigest()
            if source_kind == "synthetic" and (
                request.source is not None or previous.source_kind != "synthetic"
            ):
                base_id = _audio_base_id(
                    workflow_id=workflow.info().workflow_id,
                    source_kind=source_kind,
                    source_identity=None,
                    source_content=source_content,
                    source_hash=source_hash,
                    bridge_id=request.bridge_id,
                    root_id=request.root_id,
                    folder_binding_id=request.folder_binding_id,
                )
                source_identity = f"synthetic:{base_id}"
            draft_id = _audio_review_id(
                workflow_id=workflow.info().workflow_id,
                source_kind=source_kind,
                source_identity=source_identity,
                source_content=source_content,
                source_hash=source_hash,
                recap_script=recap_script,
                wav_path=wav_path,
                synthetic_markdown_path=synthetic_markdown_path,
                bridge_id=request.bridge_id,
                root_id=request.root_id,
                folder_binding_id=request.folder_binding_id,
            )
            self._audio_package = None
            self._audio_draft = AudioDraft(
                draft_id=draft_id,
                source_kind=source_kind,
                source_identity=source_identity,
                source_content=source_content,
                source_hash=source_hash,
                recap_script=recap_script,
                wav_path=wav_path,
                synthetic_markdown_path=synthetic_markdown_path,
                bridge_id=request.bridge_id,
                root_id=request.root_id,
                folder_binding_id=request.folder_binding_id,
            )
            return AudioDraftResponse(draft=self._audio_draft)
        source_content, recap_script = await self._prepare_audio_with_model(request)
        assert request.source is not None
        workflow_id = workflow.info().workflow_id
        if isinstance(request.source, ExistingTranscriptSource):
            source_identity = request.source.source_identity
            source_hash = request.source.source_hash
            synthetic_markdown_path = None
        else:
            source_hash = hashlib.sha256(source_content.encode()).hexdigest()
            source_identity = None
        base_id = _audio_base_id(
            workflow_id=workflow_id,
            source_kind=request.source.source_kind,
            source_identity=source_identity,
            source_content=source_content,
            source_hash=source_hash,
            bridge_id=request.bridge_id,
            root_id=request.root_id,
            folder_binding_id=request.folder_binding_id,
        )
        if source_identity is None:
            source_identity = f"synthetic:{base_id}"
            synthetic_markdown_path = f"audio/{base_id}.md"
        wav_path = f"audio/{base_id}.wav"
        draft_id = _audio_review_id(
            workflow_id=workflow_id,
            source_kind=request.source.source_kind,
            source_identity=source_identity,
            source_content=source_content,
            source_hash=source_hash,
            recap_script=recap_script,
            wav_path=wav_path,
            synthetic_markdown_path=synthetic_markdown_path,
            bridge_id=request.bridge_id,
            root_id=request.root_id,
            folder_binding_id=request.folder_binding_id,
        )
        self._audio_package = None
        self._audio_draft = AudioDraft(
            draft_id=draft_id,
            source_kind=request.source.source_kind,
            source_identity=source_identity,
            source_content=source_content,
            source_hash=source_hash,
            recap_script=recap_script,
            wav_path=wav_path,
            synthetic_markdown_path=synthetic_markdown_path,
            bridge_id=request.bridge_id,
            root_id=request.root_id,
            folder_binding_id=request.folder_binding_id,
        )
        return AudioDraftResponse(draft=self._audio_draft)

    async def _prepare_audio_with_model(
        self, request: PrepareAudioRequest
    ) -> tuple[str, str]:
        """Ask the parent model for the exact transcript/recap fields and validate its JSON."""
        source = request.source
        if isinstance(source, ExistingTranscriptSource):
            prompt = (
                "Return JSON with one string field, recap_script, containing a concise spoken "
                "D&D recap of this transcript. Do not include Markdown or other fields.\n\n"
                + source.source_content
            )
        else:
            prompt = (
                "Return JSON with string fields source_content and recap_script. source_content "
                "must be a clearly fictional D&D transcript beginning exactly with "
                "'# Synthetic Transcript\\n'; recap_script must be a concise spoken recap. "
                f"Topic: {source.topic}"
            )
        reply, calls, _ = await self._execute_agent_interaction(
            gemini=self._gemini,
            model=self._model,
            input=prompt,
            tools=[],
            system_instruction="Return only the requested JSON object.",
            previous_interaction_id=None,
        )
        if calls:
            raise ValueError("audio preparation model must not request tools")
        prepared = json.loads(reply)
        if isinstance(source, ExistingTranscriptSource):
            recap_script = prepared.get("recap_script")
            if not isinstance(recap_script, str) or not recap_script.strip():
                raise ValueError("recap_script must be a non-empty string")
            return source.source_content, recap_script
        synthetic = _SyntheticAudioPreparation.model_validate(prepared)
        return synthetic.source_content, synthetic.recap_script

    async def _reprepare_audio_with_model(
        self,
        request: PrepareAudioRequest,
        previous: AudioDraft,
    ) -> tuple[str, str, str, str | None]:
        """Ask for a complete revised review payload from explicit prior context."""
        selected_source = (
            request.source.model_dump(mode="json") if request.source is not None else None
        )
        prompt = (
            "Return JSON with exactly source_content, recap_script, wav_path, and "
            "synthetic_markdown_path. Apply only the requested change and preserve every "
            "unaffected field from the prior draft.\n"
            f"Requested change: {request.change_request}\n"
            "Prior full draft: "
            f"{json.dumps(previous.model_dump(mode='json'), sort_keys=True)}\n"
            f"Separately selected source: {json.dumps(selected_source, sort_keys=True)}"
        )
        reply, calls, _ = await self._execute_agent_interaction(
            gemini=self._gemini,
            model=self._model,
            input=prompt,
            tools=[],
            system_instruction="Return only the requested JSON object.",
            previous_interaction_id=None,
        )
        if calls:
            raise ValueError("audio repreparation model must not request tools")
        prepared = _AudioDraftRepreparation.model_validate_json(reply)
        existing_source_content: str | None = None
        if isinstance(request.source, ExistingTranscriptSource):
            existing_source_content = request.source.source_content
        elif request.source is None and previous.source_kind == "existing":
            existing_source_content = previous.source_content
        if (
            existing_source_content is not None
            and prepared.source_content != existing_source_content
        ):
            raise ValueError("existing transcript is immutable during repreparation")
        source_kind = (
            request.source.source_kind
            if request.source is not None
            else previous.source_kind
        )
        if source_kind == "synthetic":
            _SyntheticAudioPreparation(
                source_content=prepared.source_content,
                recap_script=prepared.recap_script,
            )
        return (
            prepared.source_content,
            prepared.recap_script,
            prepared.wav_path,
            prepared.synthetic_markdown_path,
        )

    @agent.accepts
    async def start_audio(self, request: StartAudioRequest) -> AudioGenerationResult:
        """Validate the stored review state, finalize it, then enter the approval tool."""
        draft = self._audio_draft
        if draft is None:
            raise ValueError("no prepared audio draft")
        if request.draft_id != draft.draft_id or request.draft_digest != draft.draft_digest:
            raise ValueError("start request does not match the stored audio draft")
        binding = (request.bridge_id, request.root_id, request.folder_binding_id)
        if binding != (draft.bridge_id, draft.root_id, draft.folder_binding_id):
            raise ValueError("start request binding does not match the stored audio draft")
        expected_paths = {draft.wav_path}
        if draft.synthetic_markdown_path is not None:
            expected_paths.add(draft.synthetic_markdown_path)
        if set(request.preflighted_paths) != expected_paths or len(
            request.preflighted_paths
        ) != len(expected_paths):
            raise ValueError("all and only draft destinations must be preflighted")

        generation_id = "generation-" + hashlib.sha256(
            f"{workflow.info().workflow_id}:{draft.draft_id}".encode()
        ).hexdigest()[:20]
        previous_package = self._audio_package
        candidate_package = AudioApprovalPackage(
            package_revision=1,
            generation_id=generation_id,
            source_kind=draft.source_kind,
            source_identity=draft.source_identity,
            source_content=draft.source_content,
            source_hash=draft.source_hash,
            recap_script=draft.recap_script,
            voice=draft.voice,
            wav_path=draft.wav_path,
            synthetic_markdown_path=draft.synthetic_markdown_path,
            bridge_id=draft.bridge_id,
            root_id=draft.root_id,
            folder_binding_id=draft.folder_binding_id,
        )
        self._audio_package = candidate_package
        try:
            result = await self._runner.run_tool(
                f"generate-audio:{draft.draft_id}",
                generate_audio,
                candidate_package,
            )
            self._audio_package = self._validated_child_package(
                candidate_package, result
            )
            return result
        except agent.ToolApprovalDenied:
            self._audio_package = previous_package
            raise

    @agent.accepts
    async def recover_audio(self, request: RecoverAudioRequest) -> AudioGenerationResult:
        """Restart only the exact stored approved package after its fixed child closes."""
        package = self._audio_package
        if package is None:
            raise ValueError("no approved audio package to recover")
        supplied_identity = (
            request.generation_id,
            request.content_digest,
            request.destination_digest,
            request.package_digest,
            request.bridge_id,
            request.root_id,
            request.folder_binding_id,
        )
        stored_identity = (
            package.generation_id,
            package.content_digest,
            package.destination_digest,
            package.package_digest,
            package.bridge_id,
            package.root_id,
            package.folder_binding_id,
        )
        if supplied_identity != stored_identity:
            raise ValueError("recovery request does not match the stored approved package")
        result = await launch_audio_child(
            AudioGenerationRequest(package=package, mode="recovery")
        )
        self._audio_package = self._validated_child_package(package, result)
        return result

    @staticmethod
    def _validated_child_package(
        expected: AudioApprovalPackage,
        result: AudioGenerationResult,
    ) -> AudioApprovalPackage:
        approved = getattr(result, "approved_package", None)
        if approved is None:
            return expected
        if (
            approved.generation_id,
            approved.content_digest,
            approved.bridge_id,
            approved.root_id,
            approved.folder_binding_id,
        ) != (
            expected.generation_id,
            expected.content_digest,
            expected.bridge_id,
            expected.root_id,
            expected.folder_binding_id,
        ) or approved.package_revision < expected.package_revision:
            raise ValueError("child approved package does not match parent authority")
        return approved

    def _set_model(self, model: str) -> None:
        self._model = model

    # ------------------------------------------------------------------ chat loop

    async def _handle_chat_turn(self, gemini: AsyncClient, user_text: str) -> str:
        """Run one tool-free conversational turn and retain server-side chaining."""
        reply_text, pending_calls, interaction_id = (
            await self._execute_agent_interaction(
                gemini=gemini,
                model=self._model,
                input=user_text,
                tools=[],
                system_instruction=SYSTEM_INSTRUCTION,
                previous_interaction_id=self._previous_interaction_id,
            )
        )
        if pending_calls:
            raise ApplicationError(
                "tool-free Chronicler ask returned a function call",
                type="unexpected_tool_call",
            )
        self._previous_interaction_id = interaction_id
        return reply_text

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
