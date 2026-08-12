import asyncio
import hashlib
from typing import TypeVar

import pytest
from pydantic import BaseModel, ValidationError
from temporalio.contrib.pydantic import pydantic_data_converter

from examples.chronicler.audio_models import (
    RECAP_TARGET_MAX_SECONDS,
    RECAP_TARGET_MIN_SECONDS,
    AudioApprovalPackage,
    AudioArtifactInspectionResult,
    AudioDraft,
    AudioDraftResponse,
    AudioGenerationRequest,
    AudioGenerationResult,
    AudioGenerationSnapshot,
    AudioGenerationStatus,
    ArtifactReceipt,
    CreateAudioArtifactResult,
    DestinationRevision,
    ExistingTranscriptSource,
    PrepareAudioRequest,
    RecoverAudioRequest,
    StartAudioRequest,
    SyntheticTopicSource,
    SynthesizedWav,
)

BoundaryModel = TypeVar("BoundaryModel", bound=BaseModel)


async def _round_trip(value: BoundaryModel, type_hint: type[BoundaryModel]) -> BoundaryModel:
    payloads = await pydantic_data_converter.encode([value])
    decoded = await pydantic_data_converter.decode(payloads, [type_hint])
    return decoded[0]


def test_approval_package_derives_deterministic_digests_from_approved_fields() -> None:
    transcript = "The party entered the crypt."
    approved = {
        "package_revision": 1,
        "generation_id": "generation-parent-7",
        "source_kind": "existing",
        "source_identity": "sessions/session-7/transcript.json",
        "source_content": transcript,
        "source_hash": hashlib.sha256(transcript.encode()).hexdigest(),
        "recap_script": "Previously, the party entered the crypt.",
        "voice": "Charon",
        "wav_path": "audio/session-7-recap.wav",
        "synthetic_markdown_path": None,
        "bridge_id": "bridge-a",
        "root_id": "root-a",
        "folder_binding_id": "binding-a",
    }

    first = AudioApprovalPackage(**approved)
    second = AudioApprovalPackage(**approved)

    assert first.content_digest == second.content_digest
    assert first.destination_digest == second.destination_digest
    assert first.package_digest == second.package_digest
    assert first.generation_id == "generation-parent-7"
    assert first.folder_binding_id == "binding-a"


def test_approval_package_round_trips_through_temporal_pydantic_converter() -> None:
    transcript = "The party entered the crypt."
    package = AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7/transcript.json",
        source_content=transcript,
        source_hash=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )

    decoded = asyncio.run(_round_trip(package, AudioApprovalPackage))

    assert decoded == package
    assert decoded.package_digest == package.package_digest


def test_approval_package_public_serialization_includes_approved_digests() -> None:
    transcript = "The party entered the crypt."
    package = AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7/transcript.json",
        source_content=transcript,
        source_hash=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )

    serialized = package.model_dump()

    assert serialized["content_digest"] == package.content_digest
    assert serialized["destination_digest"] == package.destination_digest
    assert serialized["package_digest"] == package.package_digest


def test_approval_package_validated_copy_recomputes_destination_digests() -> None:
    transcript = "The party entered the crypt."
    package = AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7/transcript.json",
        source_content=transcript,
        source_hash=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )

    revised = package.model_copy(
        update={
            "package_revision": 2,
            "wav_path": "audio/session-7-recap-2.wav",
        }
    )

    assert revised.content_digest == package.content_digest
    assert revised.destination_digest != package.destination_digest
    assert revised.package_digest != package.package_digest


def test_nested_generation_request_round_trips_through_temporal_converter() -> None:
    transcript = "The party entered the crypt."
    package = AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7/transcript.json",
        source_content=transcript,
        source_hash=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )
    request = AudioGenerationRequest(package=package, mode="normal")

    decoded = asyncio.run(_round_trip(request, AudioGenerationRequest))

    assert decoded == request
    assert decoded.package.package_digest == package.package_digest


def test_audio_draft_round_trips_through_temporal_pydantic_converter() -> None:
    transcript = "The party entered the crypt."
    draft = AudioDraft(
        draft_id="draft-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7/transcript.json",
        source_content=transcript,
        source_hash=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )

    decoded = asyncio.run(_round_trip(draft, AudioDraft))

    assert decoded == draft
    assert decoded.draft_digest == draft.draft_digest


def test_audio_draft_public_serialization_includes_its_digest() -> None:
    transcript = "The party entered the crypt."
    draft = AudioDraft(
        draft_id="draft-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7/transcript.json",
        source_content=transcript,
        source_hash=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )

    assert draft.model_dump()["draft_digest"] == draft.draft_digest


def test_audio_draft_validated_copy_recomputes_its_digest() -> None:
    transcript = "The party entered the crypt."
    draft = AudioDraft(
        draft_id="draft-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7/transcript.json",
        source_content=transcript,
        source_hash=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )

    revised = draft.model_copy(update={"recap_script": "A changed exact recap."})

    assert revised.draft_digest != draft.draft_digest


def test_all_remaining_boundary_models_round_trip_through_temporal_converter() -> None:
    transcript = "The party entered the crypt."
    source = ExistingTranscriptSource(
        source_identity="sessions/session-7/transcript.json",
        source_content=transcript,
        source_hash=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
    )
    draft = AudioDraft(
        draft_id="draft-parent-7",
        source_kind="existing",
        source_identity=source.source_identity,
        source_content=source.source_content,
        source_hash=source.source_hash,
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )
    status = AudioGenerationStatus(
        generation_id="generation-parent-7",
        child_workflow_id="chronicler-audio--parent-7",
        phase="complete",
    )
    approved_package = AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
        source_kind="existing",
        source_identity=source.source_identity,
        source_content=source.source_content,
        source_hash=source.source_hash,
        recap_script=draft.recap_script,
        wav_path=draft.wav_path,
        bridge_id=draft.bridge_id,
        root_id=draft.root_id,
        folder_binding_id=draft.folder_binding_id,
    )
    boundary_values: tuple[BaseModel, ...] = (
        source,
        SyntheticTopicSource(topic="the whispering crypt"),
        PrepareAudioRequest(
            source=source,
            bridge_id="bridge-a",
            root_id="root-a",
            folder_binding_id="binding-a",
        ),
        AudioDraftResponse(draft=draft),
        StartAudioRequest(
            draft_id=draft.draft_id,
            draft_digest=draft.draft_digest,
            bridge_id="bridge-a",
            root_id="root-a",
            folder_binding_id="binding-a",
            preflighted_paths=(draft.wav_path,),
        ),
        RecoverAudioRequest(
            generation_id="generation-parent-7",
            content_digest="a" * 64,
            destination_digest="b" * 64,
            package_digest="c" * 64,
            bridge_id="bridge-a",
            root_id="root-a",
            folder_binding_id="binding-a",
        ),
        status,
        AudioGenerationResult(
            generation_id="generation-parent-7",
            outcome="completed",
            status=status,
            duration_s=61.5,
            approved_package=approved_package,
        ),
        AudioGenerationSnapshot(
            child_workflow_id=status.child_workflow_id,
            state="completed",
            status=status,
            approved_package=approved_package,
            result=AudioGenerationResult(
                generation_id="generation-parent-7",
                outcome="completed",
                status=status,
                duration_s=61.5,
                approved_package=approved_package,
            ),
        ),
        ArtifactReceipt(
            generation_id="generation-parent-7",
            artifact_role="wav",
            relative_path="audio/session-7-recap.wav",
            content_hash="d" * 64,
            content_size=1_024,
            package_revision=1,
            operation_id="audio-write-generation-parent-7-wav-r1",
            folder_binding_id="binding-a",
        ),
        CreateAudioArtifactResult(
            status="created",
            relative_path="audio/session-7-recap.wav",
            observed_content_hash="d" * 64,
            content_size=1_024,
            receipt=ArtifactReceipt(
                generation_id="generation-parent-7",
                artifact_role="wav",
                relative_path="audio/session-7-recap.wav",
                content_hash="d" * 64,
                content_size=1_024,
                package_revision=1,
                operation_id="audio-write-generation-parent-7-wav-r1",
                folder_binding_id="binding-a",
            ),
        ),
        AudioArtifactInspectionResult(status="missing"),
        SynthesizedWav(
            script="Previously, the party entered the crypt.",
            voice="Charon",
            audio_base64="UklGRg==",
            wav_hash="e" * 64,
            wav_size=44,
            duration_s=1.0,
            sample_rate_hz=8_000,
            channels=1,
            sample_width_bytes=2,
        ),
    )

    for value in boundary_values:
        decoded = asyncio.run(_round_trip(value, type(value)))
        assert decoded == value


def test_prepare_audio_accepts_a_validated_existing_source_and_folder_binding() -> None:
    transcript = "The party entered the crypt."

    request = PrepareAudioRequest(
        source=ExistingTranscriptSource(
            source_identity="sessions/session-7/transcript.json",
            source_content=transcript,
            source_hash=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
        ),
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )

    assert request.source.source_kind == "existing"
    assert request.folder_binding_id == "binding-a"


def test_prepare_audio_accepts_a_topic_source_with_the_same_folder_binding_contract() -> None:
    request = PrepareAudioRequest(
        source=SyntheticTopicSource(topic="the whispering crypt"),
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )

    assert request.source.source_kind == "synthetic"
    assert request.source.topic == "the whispering crypt"


def test_prepare_audio_accepts_a_contextual_change_against_a_stored_draft() -> None:
    request = PrepareAudioRequest(
        change_request="Make the narration emphasize the final battle.",
        base_draft_digest="a" * 64,
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )

    assert request.source is None
    assert request.change_request == "Make the narration emphasize the final battle."
    assert request.base_draft_digest == "a" * 64


@pytest.mark.parametrize(
    "change_request, base_draft_digest",
    [
        ("Make the narration shorter.", None),
        (None, "a" * 64),
    ],
)
def test_prepare_audio_requires_change_request_and_base_digest_together(
    change_request: str | None,
    base_draft_digest: str | None,
) -> None:
    with pytest.raises(ValidationError, match="supplied together"):
        PrepareAudioRequest(
            change_request=change_request,
            base_draft_digest=base_draft_digest,
            bridge_id="bridge-a",
            root_id="root-a",
            folder_binding_id="binding-a",
        )


def test_prepare_audio_rejects_a_blank_change_request() -> None:
    with pytest.raises(ValidationError, match="change_request must not be blank"):
        PrepareAudioRequest(
            change_request="   ",
            base_draft_digest="a" * 64,
            bridge_id="bridge-a",
            root_id="root-a",
            folder_binding_id="binding-a",
        )


def test_initial_prepare_audio_still_requires_a_source() -> None:
    with pytest.raises(ValidationError, match="initial preparation requires a source"):
        PrepareAudioRequest(
            bridge_id="bridge-a",
            root_id="root-a",
            folder_binding_id="binding-a",
        )


def test_existing_transcript_source_rejects_a_hash_that_does_not_match_utf8_content() -> None:
    with pytest.raises(ValidationError, match="source hash"):
        ExistingTranscriptSource(
            source_identity="sessions/session-7/transcript.json",
            source_content="The party entered the crypt.",
            source_hash="0" * 64,
        )


def test_approval_package_separates_content_and_destination_revisions() -> None:
    transcript = "The party entered the crypt."
    approved = {
        "package_revision": 1,
        "generation_id": "generation-parent-7",
        "source_kind": "existing",
        "source_identity": "sessions/session-7/transcript.json",
        "source_content": transcript,
        "source_hash": hashlib.sha256(transcript.encode()).hexdigest(),
        "recap_script": "Previously, the party entered the crypt.",
        "voice": "Charon",
        "wav_path": "audio/session-7-recap.wav",
        "synthetic_markdown_path": None,
        "bridge_id": "bridge-a",
        "root_id": "root-a",
        "folder_binding_id": "binding-a",
    }
    original = AudioApprovalPackage(**approved)

    for field in (
        "source_identity",
        "source_content",
        "recap_script",
        "bridge_id",
        "root_id",
        "folder_binding_id",
    ):
        changed_value = f"changed-{field}"
        changed_fields = {field: changed_value}
        if field == "source_content":
            changed_fields["source_hash"] = hashlib.sha256(
                changed_value.encode("utf-8")
            ).hexdigest()
        changed = AudioApprovalPackage(**(approved | changed_fields))
        assert changed.content_digest != original.content_digest
        assert changed.package_digest != original.package_digest

    moved = AudioApprovalPackage(
        **(approved | {"package_revision": 2, "wav_path": "audio/retry.wav"})
    )
    assert moved.content_digest == original.content_digest
    assert moved.destination_digest != original.destination_digest
    assert moved.package_digest != original.package_digest


def test_approval_package_enforces_source_provenance_and_closed_fields() -> None:
    existing_content = "The party entered the crypt."
    existing = {
        "package_revision": 1,
        "generation_id": "generation-parent-7",
        "source_kind": "existing",
        "source_identity": "sessions/session-7/transcript.json",
        "source_content": existing_content,
        "source_hash": hashlib.sha256(existing_content.encode()).hexdigest(),
        "recap_script": "Previously, the party entered the crypt.",
        "voice": "Charon",
        "wav_path": "audio/session-7-recap.wav",
        "synthetic_markdown_path": None,
        "bridge_id": "bridge-a",
        "root_id": "root-a",
        "folder_binding_id": "binding-a",
    }
    synthetic_content = "# Synthetic Transcript\n\nA fictional party entered the crypt."
    synthetic = existing | {
        "source_kind": "synthetic",
        "source_identity": "topic:the-whispering-crypt",
        "source_content": synthetic_content,
        "source_hash": hashlib.sha256(synthetic_content.encode()).hexdigest(),
        "synthetic_markdown_path": "audio/the-whispering-crypt.md",
    }

    assert AudioApprovalPackage(**existing).synthetic_markdown_path is None
    assert AudioApprovalPackage(**synthetic).source_kind == "synthetic"
    for invalid in (
        existing | {"synthetic_markdown_path": "audio/existing.md"},
        existing | {"source_content": ""},
        existing | {"source_hash": ""},
        synthetic | {"source_content": "A transcript without a provenance heading."},
        synthetic | {"synthetic_markdown_path": None},
        synthetic | {"synthetic_markdown_path": "audio/transcript.txt"},
        existing | {"unexpected": "field"},
    ):
        with pytest.raises(ValidationError):
            AudioApprovalPackage(**invalid)


def test_approval_package_rejects_a_source_hash_that_is_not_the_utf8_content_hash() -> None:
    content = "The party entered the crypt."

    with pytest.raises(ValidationError, match="source hash"):
        AudioApprovalPackage(
            package_revision=1,
            generation_id="generation-parent-7",
            source_kind="existing",
            source_identity="sessions/session-7/transcript.json",
            source_content=content,
            source_hash="0" * 64,
            recap_script="Previously, the party entered the crypt.",
            wav_path="audio/session-7-recap.wav",
            bridge_id="bridge-a",
            root_id="root-a",
            folder_binding_id="binding-a",
        )


def test_approval_package_rejects_a_wav_destination_outside_the_connected_folder() -> None:
    content = "The party entered the crypt."

    with pytest.raises(ValidationError, match="safe relative WAV path"):
        AudioApprovalPackage(
            package_revision=1,
            generation_id="generation-parent-7",
            source_kind="existing",
            source_identity="sessions/session-7/transcript.json",
            source_content=content,
            source_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            recap_script="Previously, the party entered the crypt.",
            wav_path="../outside.wav",
            bridge_id="bridge-a",
            root_id="root-a",
            folder_binding_id="binding-a",
        )


def test_approval_package_does_not_accept_a_caller_supplied_derived_digest() -> None:
    content = "The party entered the crypt."

    with pytest.raises(ValidationError, match="content_digest"):
        AudioApprovalPackage(
            package_revision=1,
            generation_id="generation-parent-7",
            source_kind="existing",
            source_identity="sessions/session-7/transcript.json",
            source_content=content,
            source_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            recap_script="Previously, the party entered the crypt.",
            wav_path="audio/session-7-recap.wav",
            bridge_id="bridge-a",
            root_id="root-a",
            folder_binding_id="binding-a",
            content_digest="stale-client-value",
        )


def test_approval_package_requires_a_positive_revision() -> None:
    content = "The party entered the crypt."

    with pytest.raises(ValidationError, match="package_revision"):
        AudioApprovalPackage(
            package_revision=0,
            generation_id="generation-parent-7",
            source_kind="existing",
            source_identity="sessions/session-7/transcript.json",
            source_content=content,
            source_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            recap_script="Previously, the party entered the crypt.",
            wav_path="audio/session-7-recap.wav",
            bridge_id="bridge-a",
            root_id="root-a",
            folder_binding_id="binding-a",
        )


def test_synthetic_approval_package_requires_a_markdown_sibling_of_the_wav() -> None:
    content = "# Synthetic Transcript\n\nA fictional party entered the crypt."

    with pytest.raises(ValidationError, match="sibling"):
        AudioApprovalPackage(
            package_revision=1,
            generation_id="generation-parent-7",
            source_kind="synthetic",
            source_identity="topic:whispering-crypt",
            source_content=content,
            source_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            recap_script="Previously, the fictional party entered the crypt.",
            wav_path="audio/whispering-crypt-recap.wav",
            synthetic_markdown_path="other/whispering-crypt.md",
            bridge_id="bridge-a",
            root_id="root-a",
            folder_binding_id="binding-a",
        )


def test_draft_response_exposes_the_reviewable_draft_and_computed_digest() -> None:
    content = "The party entered the crypt."
    draft = AudioDraft(
        draft_id="draft-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7/transcript.json",
        source_content=content,
        source_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )

    response = AudioDraftResponse(draft=draft)

    assert response.draft.draft_id == "draft-parent-7"
    assert response.draft.draft_digest == draft.draft_digest


def test_start_audio_requires_the_reviewed_draft_identity_and_preflight_paths() -> None:
    request = StartAudioRequest(
        draft_id="draft-parent-7",
        draft_digest="a" * 64,
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
        preflighted_paths=("audio/session-7-recap.wav",),
    )

    assert request.draft_id == "draft-parent-7"
    assert request.preflighted_paths == ("audio/session-7-recap.wav",)


def test_start_audio_normalizes_browser_preflight_paths_to_an_immutable_tuple() -> None:
    request = StartAudioRequest.model_validate(
        {
            "draft_id": "draft-parent-7",
            "draft_digest": "a" * 64,
            "bridge_id": "bridge-a",
            "root_id": "root-a",
            "folder_binding_id": "binding-a",
            "preflighted_paths": [
                "audio/session-7-recap.wav",
                "audio/session-7-recap.md",
            ],
        }
    )

    assert request.preflighted_paths == (
        "audio/session-7-recap.wav",
        "audio/session-7-recap.md",
    )


def test_recovery_request_carries_the_approved_generation_and_digest_identity() -> None:
    request = RecoverAudioRequest(
        generation_id="generation-parent-7",
        content_digest="a" * 64,
        destination_digest="b" * 64,
        package_digest="c" * 64,
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )

    assert request.generation_id == "generation-parent-7"
    assert request.package_digest == "c" * 64


def test_child_lifecycle_contracts_expose_the_request_status_and_terminal_result() -> None:
    content = "The party entered the crypt."
    package = AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7/transcript.json",
        source_content=content,
        source_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )
    request = AudioGenerationRequest(package=package, mode="normal")
    status = AudioGenerationStatus(
        generation_id=package.generation_id,
        child_workflow_id="chronicler-audio--parent-7",
        phase="complete",
    )
    result = AudioGenerationResult(
        generation_id=package.generation_id,
        outcome="completed",
        status=status,
    )

    assert request.package.package_digest == package.package_digest
    assert result.status.child_workflow_id == "chronicler-audio--parent-7"


def test_completed_generation_result_exposes_measured_wav_duration() -> None:
    status = AudioGenerationStatus(
        generation_id="generation-parent-7",
        child_workflow_id="chronicler-audio--parent-7",
        phase="complete",
    )

    result = AudioGenerationResult(
        generation_id="generation-parent-7",
        outcome="completed",
        status=status,
        duration_s=73.25,
    )

    assert result.duration_s == 73.25


def test_generation_result_rejects_a_status_for_a_different_generation() -> None:
    status = AudioGenerationStatus(
        generation_id="generation-other",
        child_workflow_id="chronicler-audio--parent-7",
        phase="complete",
    )

    with pytest.raises(ValidationError, match="generation_id"):
        AudioGenerationResult(
            generation_id="generation-parent-7",
            outcome="completed",
            status=status,
        )


def test_completed_generation_result_requires_the_complete_status_phase() -> None:
    status = AudioGenerationStatus(
        generation_id="generation-parent-7",
        child_workflow_id="chronicler-audio--parent-7",
        phase="failed",
    )

    with pytest.raises(ValidationError, match="outcome.*phase"):
        AudioGenerationResult(
            generation_id="generation-parent-7",
            outcome="completed",
            status=status,
        )


def test_failed_generation_result_requires_the_failed_status_phase() -> None:
    status = AudioGenerationStatus(
        generation_id="generation-parent-7",
        child_workflow_id="chronicler-audio--parent-7",
        phase="complete",
    )

    with pytest.raises(ValidationError, match="outcome.*phase"):
        AudioGenerationResult(
            generation_id="generation-parent-7",
            outcome="failed",
            status=status,
        )


def test_canceled_generation_result_requires_the_canceled_status_phase() -> None:
    status = AudioGenerationStatus(
        generation_id="generation-parent-7",
        child_workflow_id="chronicler-audio--parent-7",
        phase="complete",
    )

    with pytest.raises(ValidationError, match="outcome.*phase"):
        AudioGenerationResult(
            generation_id="generation-parent-7",
            outcome="canceled",
            status=status,
        )


def test_recovery_needed_result_requires_a_failed_terminal_status_phase() -> None:
    status = AudioGenerationStatus(
        generation_id="generation-parent-7",
        child_workflow_id="chronicler-audio--parent-7",
        phase="complete",
    )

    with pytest.raises(ValidationError, match="outcome.*phase"):
        AudioGenerationResult(
            generation_id="generation-parent-7",
            outcome="needs_recovery",
            status=status,
        )


def test_destination_revision_and_receipt_preserve_owned_artifact_identity() -> None:
    revision = DestinationRevision(
        generation_id="generation-parent-7",
        content_digest="a" * 64,
        destination_revision=2,
        wav_path="audio/session-7-recap-2.wav",
        synthetic_markdown_path=None,
    )
    receipt = ArtifactReceipt(
        generation_id="generation-parent-7",
        artifact_role="wav",
        relative_path=revision.wav_path,
        content_hash="b" * 64,
        content_size=1_024,
        package_revision=2,
        operation_id="audio-write-generation-parent-7-wav-r2",
        folder_binding_id="binding-a",
    )

    assert revision.destination_digest
    assert receipt.relative_path == "audio/session-7-recap-2.wav"


def test_create_audio_artifact_result_carries_a_typed_ownership_receipt() -> None:
    receipt = ArtifactReceipt(
        generation_id="generation-parent-7",
        artifact_role="wav",
        relative_path="audio/session-7-recap.wav",
        content_hash="b" * 64,
        content_size=1_024,
        package_revision=1,
        operation_id="run-7.r1.wav",
        folder_binding_id="binding-a",
    )

    result = CreateAudioArtifactResult(
        status="created",
        relative_path=receipt.relative_path,
        observed_content_hash=receipt.content_hash,
        content_size=receipt.content_size,
        receipt=receipt,
    )

    assert result.receipt == receipt


def test_audio_artifact_inspection_distinguishes_missing_and_owned_results() -> None:
    receipt = ArtifactReceipt(
        generation_id="generation-parent-7",
        artifact_role="wav",
        relative_path="audio/session-7-recap.wav",
        content_hash="b" * 64,
        content_size=1_024,
        package_revision=1,
        operation_id="audio-write:generation-parent-7:r1:wav",
        folder_binding_id="binding-a",
    )

    missing = AudioArtifactInspectionResult(status="missing")
    owned = AudioArtifactInspectionResult(
        status="owned",
        receipt=receipt,
        duration_s=61.5,
    )

    assert missing.receipt is None
    assert owned.receipt == receipt
    assert owned.duration_s == 61.5


def test_audio_snapshot_exposes_the_pending_destination_revision() -> None:
    content = "The party entered the crypt."
    approved_package = AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7.md",
        source_content=content,
        source_hash=hashlib.sha256(content.encode()).hexdigest(),
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )
    revision = DestinationRevision(
        generation_id="generation-parent-7",
        content_digest="a" * 64,
        destination_revision=2,
        wav_path="audio/session-7-recap-2.wav",
    )
    status = AudioGenerationStatus(
        generation_id=revision.generation_id,
        child_workflow_id="chronicler-audio--parent-7",
        phase="destination_approval_needed",
    )

    snapshot = AudioGenerationSnapshot(
        child_workflow_id=status.child_workflow_id,
        state="running",
        status=status,
        approved_package=approved_package,
        pending_destination_revision=revision,
    )

    assert snapshot.pending_destination_revision == revision


def test_destination_revision_continues_the_package_digest_identity() -> None:
    content = "The party entered the crypt."
    approved = {
        "generation_id": "generation-parent-7",
        "source_kind": "existing",
        "source_identity": "sessions/session-7/transcript.json",
        "source_content": content,
        "source_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "recap_script": "Previously, the party entered the crypt.",
        "bridge_id": "bridge-a",
        "root_id": "root-a",
        "folder_binding_id": "binding-a",
    }
    original = AudioApprovalPackage(
        **approved,
        package_revision=1,
        wav_path="audio/session-7-recap.wav",
    )
    revised = AudioApprovalPackage(
        **approved,
        package_revision=2,
        wav_path="audio/session-7-recap-2.wav",
    )
    revision = DestinationRevision(
        generation_id="generation-parent-7",
        content_digest=original.content_digest,
        destination_revision=2,
        wav_path="audio/session-7-recap-2.wav",
    )

    assert revised.content_digest == original.content_digest
    assert revision.destination_digest == revised.destination_digest
    assert revised.package_digest != original.package_digest


def test_destination_revision_derives_the_reconstructed_full_package_digest() -> None:
    content = "The party entered the crypt."
    revised_package = AudioApprovalPackage(
        package_revision=2,
        generation_id="generation-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7/transcript.json",
        source_content=content,
        source_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap-2.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )
    revision = DestinationRevision(
        generation_id=revised_package.generation_id,
        content_digest=revised_package.content_digest,
        destination_revision=revised_package.package_revision,
        wav_path=revised_package.wav_path,
    )

    assert revision.package_digest == revised_package.package_digest


def test_destination_revision_round_trips_through_temporal_pydantic_converter() -> None:
    revision = DestinationRevision(
        generation_id="generation-parent-7",
        content_digest="a" * 64,
        destination_revision=2,
        wav_path="audio/session-7-recap-2.wav",
    )

    decoded = asyncio.run(_round_trip(revision, DestinationRevision))

    assert decoded == revision
    assert decoded.destination_digest == revision.destination_digest
    assert decoded.package_digest == revision.package_digest


def test_destination_revision_rejects_a_supplied_digest_mismatch() -> None:
    with pytest.raises(ValidationError, match="destination_digest"):
        DestinationRevision(
            generation_id="generation-parent-7",
            content_digest="a" * 64,
            destination_revision=2,
            wav_path="audio/session-7-recap-2.wav",
            destination_digest="0" * 64,
        )


def test_destination_revision_validated_copy_recomputes_derived_digests() -> None:
    revision = DestinationRevision(
        generation_id="generation-parent-7",
        content_digest="a" * 64,
        destination_revision=1,
        wav_path="audio/session-7-recap.wav",
    )

    revised = revision.model_copy(
        update={
            "destination_revision": 2,
            "wav_path": "audio/session-7-recap-2.wav",
        }
    )

    assert revised.content_digest == revision.content_digest
    assert revised.destination_digest != revision.destination_digest
    assert revised.package_digest != revision.package_digest


def test_destination_revision_rejects_wav_path_traversal() -> None:
    with pytest.raises(ValidationError, match="safe relative WAV path"):
        DestinationRevision(
            generation_id="generation-parent-7",
            content_digest="a" * 64,
            destination_revision=2,
            wav_path="../outside.wav",
        )


def test_destination_revision_rejects_an_absolute_wav_path() -> None:
    with pytest.raises(ValidationError, match="safe relative WAV path"):
        DestinationRevision(
            generation_id="generation-parent-7",
            content_digest="a" * 64,
            destination_revision=2,
            wav_path="/tmp/outside.wav",
        )


def test_destination_revision_rejects_a_non_wav_destination_extension() -> None:
    with pytest.raises(ValidationError, match="WAV path"):
        DestinationRevision(
            generation_id="generation-parent-7",
            content_digest="a" * 64,
            destination_revision=2,
            wav_path="audio/session-7-recap.mp3",
        )


def test_destination_revision_rejects_markdown_path_traversal() -> None:
    with pytest.raises(ValidationError, match="safe relative Markdown path"):
        DestinationRevision(
            generation_id="generation-parent-7",
            content_digest="a" * 64,
            destination_revision=2,
            wav_path="audio/session-7-recap.wav",
            synthetic_markdown_path="../outside.md",
        )


def test_destination_revision_rejects_an_absolute_markdown_path() -> None:
    with pytest.raises(ValidationError, match="safe relative Markdown path"):
        DestinationRevision(
            generation_id="generation-parent-7",
            content_digest="a" * 64,
            destination_revision=2,
            wav_path="audio/session-7-recap.wav",
            synthetic_markdown_path="/tmp/outside.md",
        )


def test_destination_revision_rejects_a_non_markdown_extension() -> None:
    with pytest.raises(ValidationError, match="Markdown path"):
        DestinationRevision(
            generation_id="generation-parent-7",
            content_digest="a" * 64,
            destination_revision=2,
            wav_path="audio/session-7-recap.wav",
            synthetic_markdown_path="audio/session-7-recap.txt",
        )


def test_destination_revision_rejects_a_non_sibling_markdown_path() -> None:
    with pytest.raises(ValidationError, match="sibling"):
        DestinationRevision(
            generation_id="generation-parent-7",
            content_digest="a" * 64,
            destination_revision=2,
            wav_path="audio/session-7-recap.wav",
            synthetic_markdown_path="transcripts/session-7-recap.md",
        )


def test_approval_package_uses_the_canonical_digest_golden_values() -> None:
    content = "The party entered the crypt."
    package = AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7/transcript.json",
        source_content=content,
        source_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        recap_script="Previously, the party entered the crypt.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="bridge-a",
        root_id="root-a",
        folder_binding_id="binding-a",
    )

    assert package.content_digest == (
        "b38fc874f33fd168111ba7cc8f81f66fe0814fcd33b0be2e17562dd1025d9c64"
    )
    assert package.destination_digest == (
        "a49cce6cbfb80e1d3a3b1576e66c634c018698b66ae6ed211d59af85cd80771f"
    )
    assert package.package_digest == (
        "aad7db77f7c8204f3c3c76776361edc4dd83f75a2f3f0d25dd0b3821fc7653a4"
    )


def test_prepare_audio_rejects_an_empty_folder_routing_identifier() -> None:
    transcript = "The party entered the crypt."

    with pytest.raises(ValidationError, match="folder_binding_id"):
        PrepareAudioRequest(
            source=ExistingTranscriptSource(
                source_identity="sessions/session-7/transcript.json",
                source_content=transcript,
                source_hash=hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
            ),
            bridge_id="bridge-a",
            root_id="root-a",
            folder_binding_id="",
        )


def test_recap_duration_is_a_preparation_target_not_a_result_limit() -> None:
    result = SynthesizedWav(
        script="An exact approved recap.",
        voice="Charon",
        audio_base64="UklGRg==",
        wav_hash="0" * 64,
        wav_size=44,
        duration_s=12.5,
        sample_rate_hz=24_000,
        channels=1,
        sample_width_bytes=2,
    )

    assert (RECAP_TARGET_MIN_SECONDS, RECAP_TARGET_MAX_SECONDS) == (60, 90)
    assert result.duration_s == 12.5
