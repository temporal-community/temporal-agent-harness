import hashlib
import json
from types import SimpleNamespace

import pytest

from temporal_agent_harness.harness import agent

from examples.chronicler.audio_models import (
    AudioApprovalPackage,
    AudioDraft,
    AudioGenerationRequest,
    AudioGenerationResult,
    AudioGenerationStatus,
    ExistingTranscriptSource,
    PrepareAudioRequest,
    RecoverAudioRequest,
    StartAudioRequest,
    SyntheticTopicSource,
)
from examples.chronicler.audio_tool import generate_audio
from examples.chronicler import conversational_workflow
from examples.chronicler.conversational_workflow import (
    ChroniclerAgentWorkflow,
)


def _draft() -> AudioDraft:
    source_content = "The party crossed the frozen bridge."
    return AudioDraft(
        draft_id="draft-7",
        source_kind="existing",
        source_identity="sessions/session-7.md",
        source_content=source_content,
        source_hash=hashlib.sha256(source_content.encode()).hexdigest(),
        recap_script="Previously, the party crossed the frozen bridge.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )


def _start_request(draft: AudioDraft) -> StartAudioRequest:
    return StartAudioRequest.model_validate(
        {
            "draft_id": draft.draft_id,
            "draft_digest": draft.draft_digest,
            "bridge_id": draft.bridge_id,
            "root_id": draft.root_id,
            "folder_binding_id": draft.folder_binding_id,
            "preflighted_paths": [draft.wav_path],
        }
    )


@pytest.mark.asyncio
async def test_ask_exposes_no_model_tools() -> None:
    captured = {}

    async def execute_interaction(**kwargs):
        captured.update(kwargs)
        return "Use the audio workspace to prepare a review.", [], "interaction-7"

    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._model = "gemini-3.1-flash-lite"
    parent._previous_interaction_id = None
    parent._execute_agent_interaction = execute_interaction

    reply = await parent._handle_chat_turn(object(), "Create a recap")

    assert reply == "Use the audio workspace to prepare a review."
    assert captured["tools"] == []
    assert parent._previous_interaction_id == "interaction-7"


@pytest.mark.asyncio
async def test_prepare_audio_stores_a_draft_without_starting_generation(monkeypatch) -> None:
    source_content = "The party crossed the frozen bridge."
    request = PrepareAudioRequest(
        source=ExistingTranscriptSource(
            source_identity="sessions/session-7.md",
            source_content=source_content,
            source_hash=hashlib.sha256(source_content.encode()).hexdigest(),
        ),
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )
    generation_calls: list[object] = []

    async def prepare_with_model(self, supplied):
        assert supplied == request
        return source_content, "Previously, the party crossed the frozen bridge."

    async def forbidden_generation(*args, **kwargs):
        generation_calls.append((args, kwargs))
        raise AssertionError("preparation must not generate audio")

    monkeypatch.setattr(
        ChroniclerAgentWorkflow,
        "_prepare_audio_with_model",
        prepare_with_model,
        raising=False,
    )
    monkeypatch.setattr(
        conversational_workflow,
        "generate_audio",
        forbidden_generation,
        raising=False,
    )
    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    parent = object.__new__(ChroniclerAgentWorkflow)

    response = await parent.prepare_audio(request)

    assert response.draft == parent._audio_draft
    assert response.draft.source_content == source_content
    assert generation_calls == []


@pytest.mark.asyncio
async def test_initial_narration_change_changes_draft_identity(monkeypatch) -> None:
    source_content = "The party crossed the frozen bridge."
    source = ExistingTranscriptSource(
        source_identity="sessions/session-7.md",
        source_content=source_content,
        source_hash=hashlib.sha256(source_content.encode()).hexdigest(),
    )
    request = PrepareAudioRequest(
        source=source,
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )
    narration = "Previously, the party crossed the bridge."

    async def prepare_with_model(self, supplied):
        return source_content, narration

    monkeypatch.setattr(
        ChroniclerAgentWorkflow, "_prepare_audio_with_model", prepare_with_model
    )
    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    first_parent = object.__new__(ChroniclerAgentWorkflow)
    first_parent._audio_draft = None
    first_parent._audio_package = None
    first = await first_parent.prepare_audio(request)

    narration = "Previously, a frost giant confronted the party at the bridge."
    second_parent = object.__new__(ChroniclerAgentWorkflow)
    second_parent._audio_draft = None
    second_parent._audio_package = None
    second = await second_parent.prepare_audio(request)

    assert first.draft.draft_id != second.draft.draft_id
    calls = []

    class Runner:
        async def run_tool(self, *args):
            calls.append(args)
            package = args[2]
            return AudioGenerationResult(
                generation_id=package.generation_id,
                outcome="completed",
                status=AudioGenerationStatus(
                    generation_id=package.generation_id,
                    child_workflow_id=f"chronicler-audio--{package.generation_id}",
                    phase="complete",
                ),
                approved_package=package,
            )

    first_parent._runner = Runner()
    second_parent._runner = Runner()
    await first_parent.start_audio(_start_request(first.draft))
    await second_parent.start_audio(_start_request(second.draft))

    first_package = calls[0][2]
    second_package = calls[1][2]
    assert calls[0][0] != calls[1][0]
    assert first_package.generation_id != second_package.generation_id
    assert (
        f"audio-write:{first_package.generation_id}:r1:wav"
        != f"audio-write:{second_package.generation_id}:r1:wav"
    )


@pytest.mark.asyncio
async def test_identical_initial_review_has_deterministic_identity(monkeypatch) -> None:
    source_content = "The party crossed the frozen bridge."
    source = ExistingTranscriptSource(
        source_identity="sessions/session-7.md",
        source_content=source_content,
        source_hash=hashlib.sha256(source_content.encode()).hexdigest(),
    )
    request = PrepareAudioRequest(
        source=source,
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )

    async def prepare_with_model(self, supplied):
        return source_content, "Previously, the party crossed the bridge."

    monkeypatch.setattr(
        ChroniclerAgentWorkflow, "_prepare_audio_with_model", prepare_with_model
    )
    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    parents = [object.__new__(ChroniclerAgentWorkflow) for _ in range(2)]
    for parent in parents:
        parent._audio_draft = None
        parent._audio_package = None

    first = await parents[0].prepare_audio(request)
    second = await parents[1].prepare_audio(request)

    assert first.draft.draft_id == second.draft.draft_id
    assert first.draft.wav_path == second.draft.wav_path


@pytest.mark.asyncio
async def test_prepare_audio_reprepares_a_stored_draft_from_context(monkeypatch) -> None:
    previous = _draft()
    request = PrepareAudioRequest(
        change_request="Emphasize the frost giant at the bridge.",
        base_draft_digest=previous.draft_digest,
        bridge_id=previous.bridge_id,
        root_id=previous.root_id,
        folder_binding_id=previous.folder_binding_id,
    )

    async def reprepare_with_model(self, supplied, stored):
        assert supplied == request
        assert stored == previous
        return (
            previous.source_content,
            "Previously, the party faced a frost giant on the frozen bridge.",
            previous.wav_path,
            previous.synthetic_markdown_path,
        )

    monkeypatch.setattr(
        ChroniclerAgentWorkflow,
        "_reprepare_audio_with_model",
        reprepare_with_model,
        raising=False,
    )
    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._audio_draft = previous
    parent._audio_package = object()

    response = await parent.prepare_audio(request)

    assert response.draft.source_content == previous.source_content
    assert response.draft.recap_script == (
        "Previously, the party faced a frost giant on the frozen bridge."
    )
    assert response.draft.wav_path == previous.wav_path
    assert response.draft.draft_id != previous.draft_id
    assert response.draft.draft_digest != previous.draft_digest
    assert parent._audio_package is None


@pytest.mark.asyncio
async def test_reprepare_narration_change_changes_draft_identity(monkeypatch) -> None:
    previous = _draft()
    request = PrepareAudioRequest(
        change_request="Revise the narration.",
        base_draft_digest=previous.draft_digest,
        bridge_id=previous.bridge_id,
        root_id=previous.root_id,
        folder_binding_id=previous.folder_binding_id,
    )
    narration = "Previously, the party crossed the bridge."

    async def reprepare_with_model(self, supplied, stored):
        return (
            previous.source_content,
            narration,
            previous.wav_path,
            previous.synthetic_markdown_path,
        )

    monkeypatch.setattr(
        ChroniclerAgentWorkflow, "_reprepare_audio_with_model", reprepare_with_model
    )
    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    first_parent = object.__new__(ChroniclerAgentWorkflow)
    first_parent._audio_draft = previous
    first_parent._audio_package = None
    first = await first_parent.prepare_audio(request)

    narration = "Previously, the frost giant confronted the party at the bridge."
    second_parent = object.__new__(ChroniclerAgentWorkflow)
    second_parent._audio_draft = previous
    second_parent._audio_package = None
    second = await second_parent.prepare_audio(request)

    assert first.draft.draft_id != second.draft.draft_id


@pytest.mark.asyncio
async def test_reprepare_destination_change_changes_draft_identity(monkeypatch) -> None:
    previous = _draft()
    request = PrepareAudioRequest(
        change_request="Move the reviewed WAV destination.",
        base_draft_digest=previous.draft_digest,
        bridge_id=previous.bridge_id,
        root_id=previous.root_id,
        folder_binding_id=previous.folder_binding_id,
    )
    wav_path = previous.wav_path

    async def reprepare_with_model(self, supplied, stored):
        return (
            previous.source_content,
            previous.recap_script,
            wav_path,
            previous.synthetic_markdown_path,
        )

    monkeypatch.setattr(
        ChroniclerAgentWorkflow, "_reprepare_audio_with_model", reprepare_with_model
    )
    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    first_parent = object.__new__(ChroniclerAgentWorkflow)
    first_parent._audio_draft = previous
    first_parent._audio_package = None
    first = await first_parent.prepare_audio(request)

    wav_path = "audio/reviewed-destination.wav"
    second_parent = object.__new__(ChroniclerAgentWorkflow)
    second_parent._audio_draft = previous
    second_parent._audio_package = None
    second = await second_parent.prepare_audio(request)

    assert first.draft.draft_id != second.draft.draft_id


@pytest.mark.asyncio
async def test_prepare_audio_rejects_a_change_without_a_stored_draft() -> None:
    request = PrepareAudioRequest(
        change_request="Make the narration shorter.",
        base_draft_digest="a" * 64,
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._audio_draft = None
    parent._audio_package = None

    with pytest.raises(ValueError, match="no prepared audio draft to revise"):
        await parent.prepare_audio(request)


@pytest.mark.asyncio
async def test_prepare_audio_rejects_a_stale_base_draft_digest() -> None:
    previous = _draft()
    request = PrepareAudioRequest(
        change_request="Make the narration shorter.",
        base_draft_digest="a" * 64,
        bridge_id=previous.bridge_id,
        root_id=previous.root_id,
        folder_binding_id=previous.folder_binding_id,
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._audio_draft = previous
    package_authority = object()
    parent._audio_package = package_authority

    with pytest.raises(ValueError, match="base draft does not match"):
        await parent.prepare_audio(request)

    assert parent._audio_draft == previous
    assert parent._audio_package is package_authority


@pytest.mark.asyncio
async def test_prepare_audio_rejects_a_change_from_another_folder_binding() -> None:
    previous = _draft()
    request = PrepareAudioRequest(
        change_request="Make the narration shorter.",
        base_draft_digest=previous.draft_digest,
        bridge_id=previous.bridge_id,
        root_id=previous.root_id,
        folder_binding_id="binding-other",
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._audio_draft = previous
    parent._audio_package = None

    with pytest.raises(ValueError, match="binding does not match"):
        await parent.prepare_audio(request)


@pytest.mark.asyncio
async def test_prepare_audio_replaces_source_and_invalidates_package_authority(
    monkeypatch,
) -> None:
    previous = _draft()
    new_content = "The party entered the obsidian keep."
    new_source = ExistingTranscriptSource(
        source_identity="sessions/session-8.md",
        source_content=new_content,
        source_hash=hashlib.sha256(new_content.encode()).hexdigest(),
    )
    request = PrepareAudioRequest(
        source=new_source,
        change_request="Use this newly selected session instead.",
        base_draft_digest=previous.draft_digest,
        bridge_id=previous.bridge_id,
        root_id=previous.root_id,
        folder_binding_id=previous.folder_binding_id,
    )

    async def reprepare_with_model(self, supplied, stored):
        return (
            new_content,
            "Previously, the party entered the obsidian keep.",
            "audio/session-8-recap.wav",
            None,
        )

    monkeypatch.setattr(
        ChroniclerAgentWorkflow,
        "_reprepare_audio_with_model",
        reprepare_with_model,
    )
    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._audio_draft = previous
    parent._audio_package = object()

    response = await parent.prepare_audio(request)

    assert response.draft.source_identity == new_source.source_identity
    assert response.draft.source_content == new_source.source_content
    assert response.draft.source_hash == new_source.source_hash
    assert parent._audio_package is None


@pytest.mark.asyncio
async def test_prepare_audio_change_never_starts_generation(monkeypatch) -> None:
    previous = _draft()
    request = PrepareAudioRequest(
        change_request="Make the narration shorter.",
        base_draft_digest=previous.draft_digest,
        bridge_id=previous.bridge_id,
        root_id=previous.root_id,
        folder_binding_id=previous.folder_binding_id,
    )
    generation_calls: list[tuple[object, ...]] = []

    class Runner:
        async def run_tool(self, *args):
            generation_calls.append(args)
            raise AssertionError("repreparation must not start generation")

    async def reprepare_with_model(self, supplied, stored):
        return (
            previous.source_content,
            "Previously, the party crossed the bridge.",
            previous.wav_path,
            previous.synthetic_markdown_path,
        )

    monkeypatch.setattr(
        ChroniclerAgentWorkflow,
        "_reprepare_audio_with_model",
        reprepare_with_model,
    )
    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._audio_draft = previous
    parent._audio_package = None
    parent._runner = Runner()

    await parent.prepare_audio(request)

    assert generation_calls == []


@pytest.mark.asyncio
async def test_start_audio_finalizes_then_dispatches_the_stored_draft(monkeypatch) -> None:
    draft = _draft()
    calls: list[tuple[object, ...]] = []

    class Runner:
        async def run_tool(self, *args):
            calls.append(args)
            package = args[2]
            assert parent._audio_package == package
            return AudioGenerationResult(
                generation_id=package.generation_id,
                outcome="completed",
                status=AudioGenerationStatus(
                    generation_id=package.generation_id,
                    child_workflow_id="chronicler-audio--parent/chat-7",
                    phase="complete",
                ),
            )

    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._runner = Runner()
    parent._audio_draft = draft
    parent._audio_package = None
    request = _start_request(draft)

    result = await parent.start_audio(request)

    assert calls[0][1] is generate_audio
    assert calls[0][2] == parent._audio_package
    assert result.generation_id == parent._audio_package.generation_id


@pytest.mark.asyncio
async def test_start_audio_adopts_the_child_approved_package(monkeypatch) -> None:
    draft = _draft()
    returned_package = None

    class Runner:
        async def run_tool(self, *args):
            nonlocal returned_package
            candidate = args[2]
            returned_package = candidate.model_copy(
                update={
                    "package_revision": 2,
                    "wav_path": "audio/session-7-recap-2.wav",
                }
            )
            return AudioGenerationResult(
                generation_id=candidate.generation_id,
                outcome="completed",
                status=AudioGenerationStatus(
                    generation_id=candidate.generation_id,
                    child_workflow_id="chronicler-audio--parent/chat-7",
                    phase="complete",
                ),
                approved_package=returned_package,
            )

    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._runner = Runner()
    parent._audio_draft = draft
    parent._audio_package = None

    result = await parent.start_audio(_start_request(draft))

    assert result.approved_package == returned_package
    assert parent._audio_package == returned_package


@pytest.mark.asyncio
async def test_start_audio_rejects_a_child_package_from_another_binding(
    monkeypatch,
) -> None:
    draft = _draft()
    candidate = None

    class Runner:
        async def run_tool(self, *args):
            nonlocal candidate
            candidate = args[2]
            tampered = candidate.model_copy(
                update={"folder_binding_id": "binding-other"}
            )
            return AudioGenerationResult(
                generation_id=candidate.generation_id,
                outcome="completed",
                status=AudioGenerationStatus(
                    generation_id=candidate.generation_id,
                    child_workflow_id="chronicler-audio--parent/chat-7",
                    phase="complete",
                ),
                approved_package=tampered,
            )

    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._runner = Runner()
    parent._audio_draft = draft
    parent._audio_package = None

    with pytest.raises(ValueError, match="parent authority"):
        await parent.start_audio(_start_request(draft))

    assert parent._audio_package == candidate


@pytest.mark.asyncio
async def test_recover_audio_launches_the_unchanged_stored_package(monkeypatch) -> None:
    draft = _draft()
    package = AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
        source_kind=draft.source_kind,
        source_identity=draft.source_identity,
        source_content=draft.source_content,
        source_hash=draft.source_hash,
        recap_script=draft.recap_script,
        wav_path=draft.wav_path,
        bridge_id=draft.bridge_id,
        root_id=draft.root_id,
        folder_binding_id=draft.folder_binding_id,
    )
    launched: list[AudioGenerationRequest] = []
    expected = object()

    async def launch(request: AudioGenerationRequest):
        launched.append(request)
        return expected

    monkeypatch.setattr(conversational_workflow, "launch_audio_child", launch)
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._audio_package = package
    request = RecoverAudioRequest(
        generation_id=package.generation_id,
        content_digest=package.content_digest,
        destination_digest=package.destination_digest,
        package_digest=package.package_digest,
        bridge_id=package.bridge_id,
        root_id=package.root_id,
        folder_binding_id=package.folder_binding_id,
    )

    result = await parent.recover_audio(request)

    assert result is expected
    assert launched == [AudioGenerationRequest(package=package, mode="recovery")]


@pytest.mark.asyncio
async def test_start_audio_rejects_binding_drift_before_tool_dispatch() -> None:
    draft = _draft()

    class Runner:
        async def run_tool(self, *args):
            raise AssertionError("invalid binding must not dispatch the tool")

    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._runner = Runner()
    parent._audio_draft = draft
    parent._audio_package = None
    request = StartAudioRequest(
        draft_id=draft.draft_id,
        draft_digest=draft.draft_digest,
        bridge_id=draft.bridge_id,
        root_id=draft.root_id,
        folder_binding_id="different-binding",
        preflighted_paths=(draft.wav_path,),
    )

    with pytest.raises(ValueError, match="binding"):
        await parent.start_audio(request)


@pytest.mark.asyncio
async def test_start_audio_rejects_missing_preflight_before_tool_dispatch() -> None:
    draft = _draft()

    class Runner:
        async def run_tool(self, *args):
            raise AssertionError("missing preflight must not dispatch the tool")

    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._runner = Runner()
    parent._audio_draft = draft
    parent._audio_package = None
    request = StartAudioRequest(
        draft_id=draft.draft_id,
        draft_digest=draft.draft_digest,
        bridge_id=draft.bridge_id,
        root_id=draft.root_id,
        folder_binding_id=draft.folder_binding_id,
        preflighted_paths=(),
    )

    with pytest.raises(ValueError, match="preflighted"):
        await parent.start_audio(request)


@pytest.mark.asyncio
async def test_recover_audio_rejects_package_drift_before_child_launch(monkeypatch) -> None:
    draft = _draft()
    package = AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
        source_kind=draft.source_kind,
        source_identity=draft.source_identity,
        source_content=draft.source_content,
        source_hash=draft.source_hash,
        recap_script=draft.recap_script,
        wav_path=draft.wav_path,
        bridge_id=draft.bridge_id,
        root_id=draft.root_id,
        folder_binding_id=draft.folder_binding_id,
    )

    async def forbidden_launch(request):
        raise AssertionError("drifted recovery must not launch a child")

    monkeypatch.setattr(conversational_workflow, "launch_audio_child", forbidden_launch)
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._audio_package = package
    request = RecoverAudioRequest(
        generation_id=package.generation_id,
        content_digest=package.content_digest,
        destination_digest=package.destination_digest,
        package_digest="f" * 64,
        bridge_id=package.bridge_id,
        root_id=package.root_id,
        folder_binding_id=package.folder_binding_id,
    )

    with pytest.raises(ValueError, match="stored approved package"):
        await parent.recover_audio(request)


@pytest.mark.asyncio
async def test_denied_start_cannot_be_recovered_when_no_package_was_previously_approved(
    monkeypatch,
) -> None:
    draft = _draft()
    candidate: AudioApprovalPackage | None = None

    class Runner:
        async def run_tool(self, *args):
            nonlocal candidate
            candidate = args[2]
            raise agent.ToolApprovalDenied("generate_audio", "not approved")

    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._runner = Runner()
    parent._audio_draft = draft
    parent._audio_package = None

    with pytest.raises(agent.ToolApprovalDenied):
        await parent.start_audio(_start_request(draft))

    assert candidate is not None
    recovery = RecoverAudioRequest(
        generation_id=candidate.generation_id,
        content_digest=candidate.content_digest,
        destination_digest=candidate.destination_digest,
        package_digest=candidate.package_digest,
        bridge_id=candidate.bridge_id,
        root_id=candidate.root_id,
        folder_binding_id=candidate.folder_binding_id,
    )
    with pytest.raises(ValueError, match="no approved audio package"):
        await parent.recover_audio(recovery)


@pytest.mark.asyncio
async def test_denied_start_preserves_the_previous_approved_package(monkeypatch) -> None:
    draft = _draft()
    previous = AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-previous",
        source_kind=draft.source_kind,
        source_identity=draft.source_identity,
        source_content=draft.source_content,
        source_hash=draft.source_hash,
        recap_script=draft.recap_script,
        wav_path=draft.wav_path,
        bridge_id=draft.bridge_id,
        root_id=draft.root_id,
        folder_binding_id=draft.folder_binding_id,
    )
    launched: list[AudioGenerationRequest] = []

    class Runner:
        async def run_tool(self, *args):
            raise agent.ToolApprovalDenied("generate_audio", "not approved")

    async def launch(request: AudioGenerationRequest):
        launched.append(request)
        return object()

    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    monkeypatch.setattr(conversational_workflow, "launch_audio_child", launch)
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._runner = Runner()
    parent._audio_draft = draft
    parent._audio_package = previous

    with pytest.raises(agent.ToolApprovalDenied):
        await parent.start_audio(_start_request(draft))

    await parent.recover_audio(
        RecoverAudioRequest(
            generation_id=previous.generation_id,
            content_digest=previous.content_digest,
            destination_digest=previous.destination_digest,
            package_digest=previous.package_digest,
            bridge_id=previous.bridge_id,
            root_id=previous.root_id,
            folder_binding_id=previous.folder_binding_id,
        )
    )
    assert launched == [AudioGenerationRequest(package=previous, mode="recovery")]


@pytest.mark.asyncio
async def test_post_approval_failure_retains_the_candidate_for_recovery(monkeypatch) -> None:
    draft = _draft()
    candidate: AudioApprovalPackage | None = None
    launched: list[AudioGenerationRequest] = []

    class Runner:
        async def run_tool(self, *args):
            nonlocal candidate
            candidate = args[2]
            raise RuntimeError("child failed after approval")

    async def launch(request: AudioGenerationRequest):
        launched.append(request)
        return object()

    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    monkeypatch.setattr(conversational_workflow, "launch_audio_child", launch)
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._runner = Runner()
    parent._audio_draft = draft
    parent._audio_package = None

    with pytest.raises(RuntimeError, match="child failed after approval"):
        await parent.start_audio(_start_request(draft))

    assert candidate is not None
    await parent.recover_audio(
        RecoverAudioRequest(
            generation_id=candidate.generation_id,
            content_digest=candidate.content_digest,
            destination_digest=candidate.destination_digest,
            package_digest=candidate.package_digest,
            bridge_id=candidate.bridge_id,
            root_id=candidate.root_id,
            folder_binding_id=candidate.folder_binding_id,
        )
    )
    assert launched == [AudioGenerationRequest(package=candidate, mode="recovery")]


@pytest.mark.asyncio
async def test_prepare_audio_invalidates_the_previous_recoverable_package(
    monkeypatch,
) -> None:
    previous_draft = _draft()
    previous = AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-previous",
        source_kind=previous_draft.source_kind,
        source_identity=previous_draft.source_identity,
        source_content=previous_draft.source_content,
        source_hash=previous_draft.source_hash,
        recap_script=previous_draft.recap_script,
        wav_path=previous_draft.wav_path,
        bridge_id=previous_draft.bridge_id,
        root_id=previous_draft.root_id,
        folder_binding_id=previous_draft.folder_binding_id,
    )
    source_content = "A different session entered the crystal caverns."
    request = PrepareAudioRequest(
        source=ExistingTranscriptSource(
            source_identity="sessions/session-8.md",
            source_content=source_content,
            source_hash=hashlib.sha256(source_content.encode()).hexdigest(),
        ),
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )

    async def prepare_with_model(self, supplied):
        return source_content, "Previously, the party entered the crystal caverns."

    monkeypatch.setattr(
        ChroniclerAgentWorkflow,
        "_prepare_audio_with_model",
        prepare_with_model,
    )
    monkeypatch.setattr(
        conversational_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7"),
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._audio_package = previous

    await parent.prepare_audio(request)

    with pytest.raises(ValueError, match="no approved audio package"):
        await parent.recover_audio(
            RecoverAudioRequest(
                generation_id=previous.generation_id,
                content_digest=previous.content_digest,
                destination_digest=previous.destination_digest,
                package_digest=previous.package_digest,
                bridge_id=previous.bridge_id,
                root_id=previous.root_id,
                folder_binding_id=previous.folder_binding_id,
            )
        )


@pytest.mark.asyncio
async def test_synthetic_model_output_accepts_exact_provenance_and_nonempty_recap(
    monkeypatch,
) -> None:
    transcript = "# Synthetic Transcript\nThe party entered a crystal cavern."
    recap = "Previously, the party entered the crystal caverns."

    async def execute_interaction(self, **kwargs):
        return json.dumps({"source_content": transcript, "recap_script": recap}), [], "id"

    monkeypatch.setattr(
        ChroniclerAgentWorkflow,
        "_execute_agent_interaction",
        execute_interaction,
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._gemini = object()
    parent._model = "stub-model"
    request = PrepareAudioRequest(
        source=SyntheticTopicSource(topic="crystal caverns"),
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )

    assert await parent._prepare_audio_with_model(request) == (transcript, recap)


@pytest.mark.asyncio
async def test_reprepare_model_receives_the_full_draft_and_returns_full_fields(
    monkeypatch,
) -> None:
    previous = _draft()
    request = PrepareAudioRequest(
        change_request="Shorten only the narration.",
        base_draft_digest=previous.draft_digest,
        bridge_id=previous.bridge_id,
        root_id=previous.root_id,
        folder_binding_id=previous.folder_binding_id,
    )
    seen_input: list[str] = []

    async def execute_interaction(self, **kwargs):
        seen_input.append(kwargs["input"])
        return (
            json.dumps(
                {
                    "source_content": previous.source_content,
                    "recap_script": "Previously, the party crossed the bridge.",
                    "wav_path": previous.wav_path,
                    "synthetic_markdown_path": previous.synthetic_markdown_path,
                }
            ),
            [],
            "id",
        )

    monkeypatch.setattr(
        ChroniclerAgentWorkflow,
        "_execute_agent_interaction",
        execute_interaction,
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._gemini = object()
    parent._model = "stub-model"

    prepared = await parent._reprepare_audio_with_model(request, previous)

    assert prepared == (
        previous.source_content,
        "Previously, the party crossed the bridge.",
        previous.wav_path,
        previous.synthetic_markdown_path,
    )
    assert request.change_request in seen_input[0]
    assert json.dumps(previous.model_dump(mode="json"), sort_keys=True) in seen_input[0]


@pytest.mark.asyncio
async def test_reprepare_rejects_changes_to_an_existing_transcript(monkeypatch) -> None:
    previous = _draft()
    request = PrepareAudioRequest(
        change_request="Make the narration shorter.",
        base_draft_digest=previous.draft_digest,
        bridge_id=previous.bridge_id,
        root_id=previous.root_id,
        folder_binding_id=previous.folder_binding_id,
    )

    async def execute_interaction(self, **kwargs):
        return (
            json.dumps(
                {
                    "source_content": "The model silently rewrote the archive transcript.",
                    "recap_script": "Previously, the party crossed the bridge.",
                    "wav_path": previous.wav_path,
                    "synthetic_markdown_path": None,
                }
            ),
            [],
            "id",
        )

    monkeypatch.setattr(
        ChroniclerAgentWorkflow,
        "_execute_agent_interaction",
        execute_interaction,
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._gemini = object()
    parent._model = "stub-model"

    with pytest.raises(ValueError, match="existing transcript is immutable"):
        await parent._reprepare_audio_with_model(request, previous)


@pytest.mark.asyncio
async def test_reprepare_requires_revised_synthetic_transcript_provenance(
    monkeypatch,
) -> None:
    content = "# Synthetic Transcript\nThe party entered the crystal cavern."
    previous = AudioDraft(
        draft_id="draft-synthetic",
        source_kind="synthetic",
        source_identity="synthetic:draft-synthetic",
        source_content=content,
        source_hash=hashlib.sha256(content.encode()).hexdigest(),
        recap_script="Previously, the party entered the crystal cavern.",
        wav_path="audio/draft-synthetic.wav",
        synthetic_markdown_path="audio/draft-synthetic.md",
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )
    request = PrepareAudioRequest(
        change_request="Add the dragon to the transcript.",
        base_draft_digest=previous.draft_digest,
        bridge_id=previous.bridge_id,
        root_id=previous.root_id,
        folder_binding_id=previous.folder_binding_id,
    )

    async def execute_interaction(self, **kwargs):
        return (
            json.dumps(
                {
                    "source_content": "The party met a dragon.",
                    "recap_script": "Previously, the party met a dragon.",
                    "wav_path": previous.wav_path,
                    "synthetic_markdown_path": previous.synthetic_markdown_path,
                }
            ),
            [],
            "id",
        )

    monkeypatch.setattr(
        ChroniclerAgentWorkflow,
        "_execute_agent_interaction",
        execute_interaction,
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._gemini = object()
    parent._model = "stub-model"

    with pytest.raises(ValueError, match="Synthetic Transcript"):
        await parent._reprepare_audio_with_model(request, previous)


@pytest.mark.asyncio
async def test_synthetic_model_output_rejects_missing_exact_provenance(
    monkeypatch,
) -> None:
    async def execute_interaction(self, **kwargs):
        return (
            json.dumps(
                {
                    "source_content": "The party entered a crystal cavern.",
                    "recap_script": "Previously, the party entered the crystal caverns.",
                }
            ),
            [],
            "id",
        )

    monkeypatch.setattr(
        ChroniclerAgentWorkflow,
        "_execute_agent_interaction",
        execute_interaction,
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._gemini = object()
    parent._model = "stub-model"
    request = PrepareAudioRequest(
        source=SyntheticTopicSource(topic="crystal caverns"),
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )

    with pytest.raises(ValueError, match="Synthetic Transcript"):
        await parent._prepare_audio_with_model(request)


@pytest.mark.asyncio
async def test_synthetic_model_output_rejects_a_non_string_recap(monkeypatch) -> None:
    async def execute_interaction(self, **kwargs):
        return (
            json.dumps(
                {
                    "source_content": "# Synthetic Transcript\nA crystal cavern.",
                    "recap_script": 123,
                }
            ),
            [],
            "id",
        )

    monkeypatch.setattr(
        ChroniclerAgentWorkflow,
        "_execute_agent_interaction",
        execute_interaction,
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._gemini = object()
    parent._model = "stub-model"
    request = PrepareAudioRequest(
        source=SyntheticTopicSource(topic="crystal caverns"),
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )

    with pytest.raises(ValueError, match="recap_script"):
        await parent._prepare_audio_with_model(request)


@pytest.mark.asyncio
async def test_synthetic_model_output_rejects_an_empty_recap(monkeypatch) -> None:
    async def execute_interaction(self, **kwargs):
        return (
            json.dumps(
                {
                    "source_content": "# Synthetic Transcript\nA crystal cavern.",
                    "recap_script": "   ",
                }
            ),
            [],
            "id",
        )

    monkeypatch.setattr(
        ChroniclerAgentWorkflow,
        "_execute_agent_interaction",
        execute_interaction,
    )
    parent = object.__new__(ChroniclerAgentWorkflow)
    parent._gemini = object()
    parent._model = "stub-model"
    request = PrepareAudioRequest(
        source=SyntheticTopicSource(topic="crystal caverns"),
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )

    with pytest.raises(ValueError, match="recap_script must be a non-empty string"):
        await parent._prepare_audio_with_model(request)
