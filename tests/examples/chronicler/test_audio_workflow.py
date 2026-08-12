import asyncio
import base64
import hashlib
from types import SimpleNamespace
import uuid

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.client import WorkflowExecutionStatus, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporal_agent_harness.harness.local_operations import (
    COMPLETE_LOCAL_OPERATION_UPDATE,
    PENDING_LOCAL_OPERATIONS_QUERY,
    LocalOperationRequest,
    LocalOperationResult,
    LocalOperationFailed,
    LocalOperationOutcome,
    LocalOperationStatus,
)

from examples.chronicler import audio_workflow
from examples.chronicler.audio_models import (
    AudioApprovalPackage,
    AudioArtifactInspectionResult,
    AudioDestinationApproval,
    AudioGenerationRequest,
    AudioGenerationResult,
    AudioGenerationSnapshot,
    AudioGenerationStatus,
    ArtifactReceipt,
    CreateAudioArtifactResult,
    DestinationRevision,
    SynthesizedWav,
)
from examples.chronicler.audio_workflow import ChroniclerAudioWorkflow
from examples.chronicler.audio_activities import synthesize_approved_audio

_stub_synthesis_calls: list[str] = []


@activity.defn(name="chronicler_audio_synthesize")
async def _stub_synthesize_approved_audio(
    package: AudioApprovalPackage,
) -> SynthesizedWav:
    _stub_synthesis_calls.append(package.generation_id)
    return SynthesizedWav(
        script=package.recap_script,
        voice="Charon",
        audio_base64="UklGRg==",
        wav_hash="a" * 64,
        wav_size=4,
        duration_s=1.0,
        sample_rate_hz=1,
        channels=1,
        sample_width_bytes=1,
    )


def _package() -> AudioApprovalPackage:
    source_content = "The party crossed the frozen bridge."
    return AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
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


def _synthetic_package() -> AudioApprovalPackage:
    source_content = "# Synthetic Transcript\nThe party entered the crystal cavern."
    return AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-8",
        source_kind="synthetic",
        source_identity="synthetic:draft-8",
        source_content=source_content,
        source_hash=hashlib.sha256(source_content.encode()).hexdigest(),
        recap_script="Previously, the party entered the crystal cavern.",
        wav_path="audio/session-8-recap.wav",
        synthetic_markdown_path="audio/session-8-recap.md",
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )


def _workflow(package: AudioApprovalPackage | None = None) -> ChroniclerAudioWorkflow:
    return ChroniclerAudioWorkflow(
        AudioGenerationRequest(package=package or _package(), mode="normal")
    )


async def _await_pending_operation(handle) -> LocalOperationRequest:
    for _ in range(100):
        pending = await handle.query(
            PENDING_LOCAL_OPERATIONS_QUERY,
            result_type=list[LocalOperationRequest],
        )
        if pending:
            return pending[0]
        await asyncio.sleep(0.01)
    raise AssertionError("audio child did not publish a local operation")


async def _complete_artifact_operation(handle, operation: LocalOperationRequest):
    arguments = operation.arguments
    receipt = ArtifactReceipt(
        generation_id=arguments["generation_id"],
        artifact_role=arguments["artifact_role"],
        relative_path=arguments["relative_path"],
        content_hash=arguments["expected_content_hash"],
        content_size=arguments["expected_content_size"],
        package_revision=arguments["package_revision"],
        operation_id=operation.operation_id,
        folder_binding_id=arguments["folder_binding_id"],
    )
    await handle.execute_update(
        COMPLETE_LOCAL_OPERATION_UPDATE,
        LocalOperationResult(
            operation_id=operation.operation_id,
            result=CreateAudioArtifactResult(
                status="created",
                relative_path=receipt.relative_path,
                observed_content_hash=receipt.content_hash,
                content_size=receipt.content_size,
                receipt=receipt,
            ),
        ),
    )
    return receipt


class _ImmediateOperations:
    async def run(self, request, result_type):
        arguments = request.arguments
        if request.kind == "inspect_audio_artifact":
            return AudioArtifactInspectionResult(status="missing")
        receipt = ArtifactReceipt(
            generation_id=arguments["generation_id"],
            artifact_role=arguments["artifact_role"],
            relative_path=arguments["relative_path"],
            content_hash=arguments["expected_content_hash"],
            content_size=arguments["expected_content_size"],
            package_revision=arguments["package_revision"],
            operation_id=request.operation_id,
            folder_binding_id=arguments["folder_binding_id"],
        )
        return CreateAudioArtifactResult(
            status="created",
            relative_path=receipt.relative_path,
            observed_content_hash=receipt.content_hash,
            content_size=receipt.content_size,
            receipt=receipt,
        )


@pytest.mark.asyncio
async def test_normal_child_synthesizes_the_unchanged_approved_package(monkeypatch) -> None:
    package = _package()
    captured: dict[str, object] = {}

    async def execute_activity(*args, **kwargs):
        captured["args"] = args
        return await _stub_synthesize_approved_audio(package)

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(
            workflow_id="chronicler-audio--parent/chat-7", run_id="run-7"
        ),
    )
    instance = _workflow(package)
    instance._operations = _ImmediateOperations()
    result = await instance.run(
        AudioGenerationRequest(package=package, mode="normal")
    )

    assert captured["args"][1] == package
    assert result.generation_id == package.generation_id
    assert result.status.child_workflow_id == "chronicler-audio--parent/chat-7"
    assert result.outcome == "completed"


@pytest.mark.asyncio
async def test_immediate_write_is_fulfilled_while_saving(monkeypatch) -> None:
    package = _package()
    synthesized = await _stub_synthesize_approved_audio(package)
    observed_phases = []

    class Operations(_ImmediateOperations):
        async def run(self, request, result_type):
            observed_phases.append(instance.audio_status().status.phase)
            return await super().run(request, result_type)

    async def execute_activity(*args, **kwargs):
        return synthesized

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="chronicler-audio--parent/chat-7"),
    )
    instance = _workflow(package)
    instance._operations = Operations()

    await instance.run(AudioGenerationRequest(package=package, mode="normal"))

    assert observed_phases == ["saving_wav"]


@pytest.mark.asyncio
async def test_delayed_write_remains_in_its_saving_phase(monkeypatch) -> None:
    package = _package()
    synthesized = await _stub_synthesize_approved_audio(package)
    entered = asyncio.Event()
    release = asyncio.Event()

    class Operations(_ImmediateOperations):
        async def run(self, request, result_type):
            entered.set()
            await release.wait()
            return await super().run(request, result_type)

    async def execute_activity(*args, **kwargs):
        return synthesized

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="chronicler-audio--parent/chat-7"),
    )
    instance = _workflow(package)
    instance._operations = Operations()
    running = asyncio.create_task(
        instance.run(AudioGenerationRequest(package=package, mode="normal"))
    )
    await entered.wait()
    assert instance.audio_status().status.phase == "saving_wav"

    release.set()
    await running


@pytest.mark.asyncio
async def test_synthesis_failure_returns_a_terminal_failed_result(monkeypatch) -> None:
    package = _package()

    async def execute_activity(*args, **kwargs):
        raise RuntimeError("voice provider unavailable")

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="chronicler-audio--parent/chat-7"),
    )
    instance = _workflow(package)

    result = await instance.run(AudioGenerationRequest(package=package, mode="normal"))

    assert result.outcome == "failed"
    assert result.status.phase == "failed"
    assert result.status.detail == "audio synthesis failed: voice provider unavailable"
    assert result.approved_package == package
    assert instance.audio_status().result == result


@pytest.mark.asyncio
async def test_recovery_child_preserves_the_same_package_and_generation(monkeypatch) -> None:
    package = _package()
    captured: list[object] = []

    async def execute_activity(*args, **kwargs):
        captured.append(args[1])
        return await _stub_synthesize_approved_audio(package)

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(
            workflow_id="chronicler-audio--parent/chat-7", run_id="run-7"
        ),
    )
    instance = _workflow(package)
    instance._operations = _ImmediateOperations()
    result = await instance.run(
        AudioGenerationRequest(package=package, mode="recovery")
    )

    assert captured == [package]
    assert result.generation_id == package.generation_id


@pytest.mark.asyncio
async def test_recovery_with_owned_wav_completes_without_synthesis(monkeypatch) -> None:
    package = _package()
    synthesis_calls = []
    requests = []
    receipts = []

    class Operations:
        async def run(self, request, result_type):
            requests.append((request, result_type))
            receipt = ArtifactReceipt(
                generation_id=package.generation_id,
                artifact_role="wav",
                relative_path=package.wav_path,
                content_hash="a" * 64,
                content_size=4,
                package_revision=package.package_revision,
                operation_id="audio-write:generation-parent-7:r1:wav",
                folder_binding_id=package.folder_binding_id,
            )
            receipts.append(receipt)
            return AudioArtifactInspectionResult(
                status="owned",
                receipt=receipt,
                duration_s=1.0,
            )

    async def execute_activity(*args, **kwargs):
        synthesis_calls.append(args)
        raise AssertionError("owned recovery must not synthesize")

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="chronicler-audio--parent/chat-7"),
    )
    instance = _workflow(package)
    instance._operations = Operations()

    result = await instance.run(AudioGenerationRequest(package=package, mode="recovery"))

    request, result_type = requests[0]
    assert request.kind == "inspect_audio_artifact"
    assert request.arguments == {
        "generation_id": package.generation_id,
        "artifact_role": "wav",
        "relative_path": package.wav_path,
        "folder_binding_id": package.folder_binding_id,
        "approved_package_revision": package.package_revision,
    }
    assert result_type is AudioArtifactInspectionResult
    assert synthesis_calls == []
    assert result.duration_s == 1.0
    assert instance.audio_status().receipts == tuple(receipts)


@pytest.mark.asyncio
async def test_recovery_accepts_unchanged_wav_receipt_from_an_older_revision(
    monkeypatch,
) -> None:
    package = _package().model_copy(update={"package_revision": 2})
    receipt = ArtifactReceipt(
        generation_id=package.generation_id,
        artifact_role="wav",
        relative_path=package.wav_path,
        content_hash="a" * 64,
        content_size=4,
        package_revision=1,
        operation_id=f"audio-write:{package.generation_id}:r1:wav",
        folder_binding_id=package.folder_binding_id,
    )

    class Operations:
        async def run(self, request, result_type):
            return AudioArtifactInspectionResult(
                status="owned", receipt=receipt, duration_s=1.0
            )

    async def execute_activity(*args, **kwargs):
        raise AssertionError("unchanged owned WAV must not be synthesized")

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="chronicler-audio--parent/chat-7"),
    )
    instance = _workflow(package)
    instance._operations = Operations()

    result = await instance.run(AudioGenerationRequest(package=package, mode="recovery"))

    assert result.outcome == "completed"
    assert instance.audio_status().receipts == (receipt,)


@pytest.mark.asyncio
async def test_recovery_with_missing_markdown_writes_only_markdown(monkeypatch) -> None:
    package = _synthetic_package()
    requests = []

    class Operations:
        async def run(self, request, result_type):
            requests.append(request)
            role = request.arguments["artifact_role"]
            if request.kind == "inspect_audio_artifact":
                if role == "synthetic_transcript":
                    return AudioArtifactInspectionResult(status="missing")
                return AudioArtifactInspectionResult(
                    status="owned",
                    receipt=ArtifactReceipt(
                        generation_id=package.generation_id,
                        artifact_role="wav",
                        relative_path=package.wav_path,
                        content_hash="a" * 64,
                        content_size=4,
                        package_revision=package.package_revision,
                        operation_id="audio-write:generation-parent-8:r1:wav",
                        folder_binding_id=package.folder_binding_id,
                    ),
                    duration_s=1.0,
                )
            arguments = request.arguments
            receipt = ArtifactReceipt(
                generation_id=package.generation_id,
                artifact_role="synthetic_transcript",
                relative_path=arguments["relative_path"],
                content_hash=arguments["expected_content_hash"],
                content_size=arguments["expected_content_size"],
                package_revision=package.package_revision,
                operation_id=request.operation_id,
                folder_binding_id=package.folder_binding_id,
            )
            return CreateAudioArtifactResult(
                status="created",
                relative_path=receipt.relative_path,
                observed_content_hash=receipt.content_hash,
                content_size=receipt.content_size,
                receipt=receipt,
            )

    async def execute_activity(*args, **kwargs):
        raise AssertionError("owned WAV recovery must not synthesize")

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="chronicler-audio--parent/chat-8"),
    )
    instance = _workflow(package)
    instance._operations = Operations()

    result = await instance.run(AudioGenerationRequest(package=package, mode="recovery"))

    assert [(request.kind, request.arguments["artifact_role"]) for request in requests] == [
        ("inspect_audio_artifact", "wav"),
        ("inspect_audio_artifact", "synthetic_transcript"),
        ("create_audio_artifact", "synthetic_transcript"),
    ]
    assert result.duration_s == 1.0


@pytest.mark.parametrize(
    "override",
    [
        {"generation_id": "generation-other"},
        {"artifact_role": "synthetic_transcript"},
        {"relative_path": "audio/other.wav"},
        {"package_revision": 99},
        {"operation_id": "audio-write:other:r99:wav"},
        {"folder_binding_id": "binding-other"},
    ],
)
@pytest.mark.asyncio
async def test_recovery_rejects_owned_receipts_from_other_authority(
    monkeypatch,
    override,
) -> None:
    package = _package()
    receipt_fields = {
        "generation_id": package.generation_id,
        "artifact_role": "wav",
        "relative_path": package.wav_path,
        "content_hash": "a" * 64,
        "content_size": 4,
        "package_revision": package.package_revision,
        "operation_id": "audio-write:generation-parent-7:r1:wav",
        "folder_binding_id": package.folder_binding_id,
    }
    receipt_fields.update(override)

    class Operations:
        async def run(self, request, result_type):
            return AudioArtifactInspectionResult(
                status="owned",
                receipt=ArtifactReceipt(**receipt_fields),
                duration_s=1.0,
            )

    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="chronicler-audio--parent/chat-7"),
    )
    instance = _workflow(package)
    instance._operations = Operations()

    result = await instance.run(AudioGenerationRequest(package=package, mode="recovery"))

    assert result.outcome == "needs_recovery"
    assert result.status.phase == "failed"
    assert instance.audio_status().receipts == ()


@pytest.mark.parametrize(
    "override",
    [
        {"generation_id": "generation-other"},
        {"artifact_role": "synthetic_transcript"},
        {"relative_path": "audio/other.wav"},
        {"content_hash": "f" * 64},
        {"content_size": 999},
        {"package_revision": 99},
        {"operation_id": "audio-write:other:r99:wav"},
        {"folder_binding_id": "binding-other"},
    ],
)
@pytest.mark.asyncio
async def test_create_rejects_receipts_from_other_authority(monkeypatch, override) -> None:
    package = _package()
    synthesized = await _stub_synthesize_approved_audio(package)

    class Operations:
        async def run(self, request, result_type):
            arguments = request.arguments
            receipt_fields = {
                "generation_id": package.generation_id,
                "artifact_role": "wav",
                "relative_path": arguments["relative_path"],
                "content_hash": arguments["expected_content_hash"],
                "content_size": arguments["expected_content_size"],
                "package_revision": package.package_revision,
                "operation_id": request.operation_id,
                "folder_binding_id": package.folder_binding_id,
            }
            receipt_fields.update(override)
            receipt = ArtifactReceipt(**receipt_fields)
            return CreateAudioArtifactResult(
                status="created",
                relative_path=receipt.relative_path,
                observed_content_hash=receipt.content_hash,
                content_size=receipt.content_size,
                receipt=receipt,
            )

    async def execute_activity(*args, **kwargs):
        return synthesized

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="chronicler-audio--parent/chat-7"),
    )
    instance = _workflow(package)
    instance._operations = Operations()

    result = await instance.run(AudioGenerationRequest(package=package, mode="normal"))

    assert result.outcome == "needs_recovery"
    assert instance.audio_status().receipts == ()


@pytest.mark.asyncio
async def test_child_persists_the_synthesized_wav_and_returns_its_receipt(
    monkeypatch,
) -> None:
    package = _package()
    synthesized = await _stub_synthesize_approved_audio(package)
    requests = []
    receipts = []

    class Operations:
        async def run(self, request, result_type):
            requests.append((request, result_type))
            receipt = ArtifactReceipt(
                generation_id=package.generation_id,
                artifact_role="wav",
                relative_path=package.wav_path,
                content_hash=synthesized.wav_hash,
                content_size=synthesized.wav_size,
                package_revision=package.package_revision,
                operation_id=request.operation_id,
                folder_binding_id=package.folder_binding_id,
            )
            receipts.append(receipt)
            return CreateAudioArtifactResult(
                status="created",
                relative_path=receipt.relative_path,
                observed_content_hash=receipt.content_hash,
                content_size=receipt.content_size,
                receipt=receipt,
            )

    async def execute_activity(*args, **kwargs):
        return synthesized

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(
            workflow_id="chronicler-audio--parent/chat-7",
            run_id="run-7",
        ),
    )
    instance = _workflow(package)
    instance._operations = Operations()

    result = await instance.run(AudioGenerationRequest(package=package, mode="normal"))

    request, result_type = requests[0]
    assert request.kind == "create_audio_artifact"
    assert request.arguments == {
        "operation_id": request.operation_id,
        "generation_id": package.generation_id,
        "artifact_role": "wav",
        "relative_path": package.wav_path,
        "content_base64": synthesized.audio_base64,
        "expected_content_hash": synthesized.wav_hash,
        "expected_content_size": synthesized.wav_size,
        "folder_binding_id": package.folder_binding_id,
        "package_revision": package.package_revision,
    }
    assert result_type is CreateAudioArtifactResult
    assert instance.audio_status().receipts == tuple(receipts)
    assert result.duration_s == synthesized.duration_s


@pytest.mark.asyncio
async def test_child_persists_the_optional_synthetic_transcript(monkeypatch) -> None:
    package = _synthetic_package()
    synthesized = await _stub_synthesize_approved_audio(package)
    requests = []

    class Operations:
        async def run(self, request, result_type):
            requests.append(request)
            arguments = request.arguments
            receipt = ArtifactReceipt(
                generation_id=package.generation_id,
                artifact_role=arguments["artifact_role"],
                relative_path=arguments["relative_path"],
                content_hash=arguments["expected_content_hash"],
                content_size=arguments["expected_content_size"],
                package_revision=package.package_revision,
                operation_id=request.operation_id,
                folder_binding_id=package.folder_binding_id,
            )
            return CreateAudioArtifactResult(
                status="created",
                relative_path=receipt.relative_path,
                observed_content_hash=receipt.content_hash,
                content_size=receipt.content_size,
                receipt=receipt,
            )

    async def execute_activity(*args, **kwargs):
        return synthesized

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(
            workflow_id="chronicler-audio--parent/chat-8",
            run_id="run-8",
        ),
    )
    instance = _workflow(package)
    instance._operations = Operations()

    await instance.run(AudioGenerationRequest(package=package, mode="normal"))

    assert [request.arguments["artifact_role"] for request in requests] == [
        "wav",
        "synthetic_transcript",
    ]
    markdown = requests[1].arguments
    encoded = base64.b64encode(package.source_content.encode("utf-8")).decode("ascii")
    assert markdown["relative_path"] == package.synthetic_markdown_path
    assert markdown["content_base64"] == encoded
    assert markdown["expected_content_hash"] == package.source_hash
    assert markdown["expected_content_size"] == len(package.source_content.encode("utf-8"))


@pytest.mark.asyncio
async def test_temporal_child_accepts_a_typed_pending_operation_completion() -> None:
    environment = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"audio-local-operation-{uuid.uuid4()}"
    workflow_id = f"chronicler-audio--parent/{uuid.uuid4()}"
    package = _package()
    try:
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ChroniclerAudioWorkflow],
            activities=[_stub_synthesize_approved_audio],
            workflow_failure_exception_types=[Exception],
        ):
            handle = await environment.client.start_workflow(
                ChroniclerAudioWorkflow.run,
                AudioGenerationRequest(package=package, mode="normal"),
                id=workflow_id,
                task_queue=task_queue,
            )
            pending: list[LocalOperationRequest] = []
            for _ in range(100):
                pending = await handle.query(
                    PENDING_LOCAL_OPERATIONS_QUERY,
                    result_type=list[LocalOperationRequest],
                )
                if pending:
                    break
                await asyncio.sleep(0.01)
            operation = pending[0]
            arguments = operation.arguments
            receipt = ArtifactReceipt(
                generation_id=arguments["generation_id"],
                artifact_role=arguments["artifact_role"],
                relative_path=arguments["relative_path"],
                content_hash=arguments["expected_content_hash"],
                content_size=arguments["expected_content_size"],
                package_revision=arguments["package_revision"],
                operation_id=operation.operation_id,
                folder_binding_id=arguments["folder_binding_id"],
            )
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    COMPLETE_LOCAL_OPERATION_UPDATE,
                    LocalOperationResult(
                        operation_id=operation.operation_id,
                        result={},
                    ),
                )
            await handle.execute_update(
                COMPLETE_LOCAL_OPERATION_UPDATE,
                LocalOperationResult(
                    operation_id=operation.operation_id,
                    result=CreateAudioArtifactResult(
                        status="created",
                        relative_path=receipt.relative_path,
                        observed_content_hash=receipt.content_hash,
                        content_size=receipt.content_size,
                        receipt=receipt,
                    ),
                ),
            )
            result = await handle.result()
            snapshot = await handle.query(
                audio_workflow.AUDIO_STATUS_QUERY,
                result_type=AudioGenerationSnapshot,
            )
    finally:
        await environment.shutdown()

    assert operation.kind == "create_audio_artifact"
    assert result.duration_s == 1.0
    assert snapshot.receipts == (receipt,)


@pytest.mark.asyncio
async def test_collision_proposes_and_resumes_an_exact_destination_revision(
    monkeypatch,
) -> None:
    package = _package()
    synthesized = await _stub_synthesize_approved_audio(package)
    activity_calls = 0
    requests = []

    class Operations:
        async def run(self, request, result_type):
            requests.append(request)
            if len(requests) == 1:
                raise LocalOperationFailed(
                    LocalOperationOutcome(
                        operation_id=request.operation_id,
                        status=LocalOperationStatus.FAILED,
                        error="audio_artifact_collision: destination exists",
                    )
                )
            arguments = request.arguments
            receipt = ArtifactReceipt(
                generation_id=arguments["generation_id"],
                artifact_role=arguments["artifact_role"],
                relative_path=arguments["relative_path"],
                content_hash=arguments["expected_content_hash"],
                content_size=arguments["expected_content_size"],
                package_revision=arguments["package_revision"],
                operation_id=request.operation_id,
                folder_binding_id=arguments["folder_binding_id"],
            )
            return CreateAudioArtifactResult(
                status="created",
                relative_path=receipt.relative_path,
                observed_content_hash=receipt.content_hash,
                content_size=receipt.content_size,
                receipt=receipt,
            )

    async def execute_activity(*args, **kwargs):
        nonlocal activity_calls
        activity_calls += 1
        return synthesized

    async def wait_condition(condition, **kwargs):
        while not condition():
            await asyncio.sleep(0)

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(audio_workflow.workflow, "wait_condition", wait_condition)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(
            workflow_id="chronicler-audio--parent/chat-7",
            run_id="run-7",
        ),
    )
    instance = _workflow(package)
    instance._operations = Operations()

    running = asyncio.create_task(
        instance.run(AudioGenerationRequest(package=package, mode="normal"))
    )
    await asyncio.sleep(0)
    for _ in range(100):
        proposal = instance.audio_status().pending_destination_revision
        if proposal is not None:
            break
        await asyncio.sleep(0)

    awaiting_approval = instance.audio_status()
    assert awaiting_approval.result is None
    assert awaiting_approval.approved_package == package
    assert proposal.wav_path == "audio/session-7-recap-2.wav"
    instance.approve_destination(
        AudioDestinationApproval(
            revision=proposal,
            bridge_id=package.bridge_id,
            root_id=package.root_id,
            folder_binding_id=package.folder_binding_id,
        )
    )
    result = await running

    assert result.outcome == "completed"
    assert activity_calls == 1
    assert [request.arguments["relative_path"] for request in requests] == [
        package.wav_path,
        proposal.wav_path,
    ]


@pytest.mark.asyncio
async def test_collision_lookalike_is_a_recovery_failure(monkeypatch) -> None:
    package = _package()
    synthesized = await _stub_synthesize_approved_audio(package)

    class Operations:
        async def run(self, request, result_type):
            raise LocalOperationFailed(
                LocalOperationOutcome(
                    operation_id=request.operation_id,
                    status=LocalOperationStatus.FAILED,
                    error="not_audio_artifact_collision: unrelated failure",
                )
            )

    async def execute_activity(*args, **kwargs):
        return synthesized

    async def wait_condition(*args, **kwargs):
        raise AssertionError("lookalike errors must not request destination approval")

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(audio_workflow.workflow, "wait_condition", wait_condition)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="chronicler-audio--parent/chat-7"),
    )
    instance = _workflow(package)
    instance._operations = Operations()

    result = await instance.run(AudioGenerationRequest(package=package, mode="normal"))

    assert result.outcome == "needs_recovery"
    assert instance.audio_status().pending_destination_revision is None


@pytest.mark.asyncio
async def test_temporal_collision_approval_resumes_without_resynthesis() -> None:
    _stub_synthesis_calls.clear()
    environment = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"audio-collision-{uuid.uuid4()}"
    workflow_id = f"chronicler-audio--parent/{uuid.uuid4()}"
    package = _package()
    try:
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ChroniclerAudioWorkflow],
            activities=[_stub_synthesize_approved_audio],
            workflow_failure_exception_types=[Exception],
        ):
            handle = await environment.client.start_workflow(
                ChroniclerAudioWorkflow.run,
                AudioGenerationRequest(package=package, mode="normal"),
                id=workflow_id,
                task_queue=task_queue,
            )
            first = await _await_pending_operation(handle)
            await handle.execute_update(
                COMPLETE_LOCAL_OPERATION_UPDATE,
                LocalOperationResult(
                    operation_id=first.operation_id,
                    error="audio_artifact_collision: destination exists",
                ),
            )
            proposal = None
            for _ in range(100):
                snapshot = await handle.query(
                    audio_workflow.AUDIO_STATUS_QUERY,
                    result_type=AudioGenerationSnapshot,
                )
                proposal = snapshot.pending_destination_revision
                if proposal is not None:
                    break
                await asyncio.sleep(0.01)
            await handle.execute_update(
                audio_workflow.APPROVE_AUDIO_DESTINATION_UPDATE,
                AudioDestinationApproval(
                    revision=proposal,
                    bridge_id=package.bridge_id,
                    root_id=package.root_id,
                    folder_binding_id=package.folder_binding_id,
                ),
            )
            retry = await _await_pending_operation(handle)
            await _complete_artifact_operation(handle, retry)
            result = await handle.result()
    finally:
        await environment.shutdown()

    assert first.arguments["relative_path"] == package.wav_path
    assert retry.arguments["relative_path"] == proposal.wav_path
    assert result.outcome == "completed"
    assert _stub_synthesis_calls == [package.generation_id]


@pytest.mark.asyncio
async def test_temporal_markdown_collision_retries_only_markdown() -> None:
    _stub_synthesis_calls.clear()
    package = _synthetic_package()
    environment = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"audio-markdown-collision-{uuid.uuid4()}"
    try:
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ChroniclerAudioWorkflow],
            activities=[_stub_synthesize_approved_audio],
            workflow_failure_exception_types=[Exception],
        ):
            handle = await environment.client.start_workflow(
                ChroniclerAudioWorkflow.run,
                AudioGenerationRequest(package=package, mode="normal"),
                id="chronicler-audio--parent/chat-8",
                task_queue=task_queue,
            )
            wav = await _await_pending_operation(handle)
            wav_receipt = await _complete_artifact_operation(handle, wav)
            markdown = await _await_pending_operation(handle)
            await handle.execute_update(
                COMPLETE_LOCAL_OPERATION_UPDATE,
                LocalOperationResult(
                    operation_id=markdown.operation_id,
                    error="audio_artifact_collision: destination exists",
                ),
            )
            proposal = None
            for _ in range(100):
                snapshot = await handle.query(
                    audio_workflow.AUDIO_STATUS_QUERY,
                    result_type=AudioGenerationSnapshot,
                )
                proposal = snapshot.pending_destination_revision
                if proposal is not None:
                    break
                await asyncio.sleep(0.01)
            await handle.execute_update(
                audio_workflow.APPROVE_AUDIO_DESTINATION_UPDATE,
                AudioDestinationApproval(
                    revision=proposal,
                    bridge_id=package.bridge_id,
                    root_id=package.root_id,
                    folder_binding_id=package.folder_binding_id,
                ),
            )
            retry = await _await_pending_operation(handle)
            markdown_receipt = await _complete_artifact_operation(handle, retry)
            result = await handle.result()
            snapshot = await handle.query(
                audio_workflow.AUDIO_STATUS_QUERY,
                result_type=AudioGenerationSnapshot,
            )
            recovery_handle = await environment.client.start_workflow(
                ChroniclerAudioWorkflow.run,
                AudioGenerationRequest(
                    package=result.approved_package, mode="recovery"
                ),
                id="chronicler-audio--parent/chat-8",
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
            inspect_wav = await _await_pending_operation(recovery_handle)
            await recovery_handle.execute_update(
                COMPLETE_LOCAL_OPERATION_UPDATE,
                LocalOperationResult(
                    operation_id=inspect_wav.operation_id,
                    result=AudioArtifactInspectionResult(
                        status="owned", receipt=wav_receipt, duration_s=1.0
                    ),
                ),
            )
            inspect_markdown = await _await_pending_operation(recovery_handle)
            await recovery_handle.execute_update(
                COMPLETE_LOCAL_OPERATION_UPDATE,
                LocalOperationResult(
                    operation_id=inspect_markdown.operation_id,
                    result=AudioArtifactInspectionResult(
                        status="owned", receipt=markdown_receipt
                    ),
                ),
            )
            recovered = await recovery_handle.result()
    finally:
        await environment.shutdown()

    assert proposal.wav_path == package.wav_path
    assert retry.arguments["artifact_role"] == "synthetic_transcript"
    assert _stub_synthesis_calls == [package.generation_id]
    assert result.approved_package.wav_path == package.wav_path
    assert snapshot.approved_package == result.approved_package
    assert snapshot.receipts[0] == wav_receipt
    assert recovered.outcome == "completed"
    assert [inspect_wav.kind, inspect_markdown.kind] == [
        "inspect_audio_artifact",
        "inspect_audio_artifact",
    ]
    assert _stub_synthesis_calls == [package.generation_id]


@pytest.mark.asyncio
async def test_collision_revision_replaces_partial_receipts_by_artifact_role(
    monkeypatch,
) -> None:
    package = _synthetic_package()
    synthesized = await _stub_synthesize_approved_audio(package)
    calls = 0
    requests = []

    class Operations:
        async def run(self, request, result_type):
            nonlocal calls
            calls += 1
            requests.append(request)
            if calls == 2:
                raise LocalOperationFailed(
                    LocalOperationOutcome(
                        operation_id=request.operation_id,
                        status=LocalOperationStatus.FAILED,
                        error="audio_artifact_collision: destination exists",
                    )
                )
            arguments = request.arguments
            receipt = ArtifactReceipt(
                generation_id=arguments["generation_id"],
                artifact_role=arguments["artifact_role"],
                relative_path=arguments["relative_path"],
                content_hash=arguments["expected_content_hash"],
                content_size=arguments["expected_content_size"],
                package_revision=arguments["package_revision"],
                operation_id=request.operation_id,
                folder_binding_id=arguments["folder_binding_id"],
            )
            return CreateAudioArtifactResult(
                status="created",
                relative_path=receipt.relative_path,
                observed_content_hash=receipt.content_hash,
                content_size=receipt.content_size,
                receipt=receipt,
            )

    async def execute_activity(*args, **kwargs):
        return synthesized

    async def wait_condition(condition, **kwargs):
        while not condition():
            await asyncio.sleep(0)

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(audio_workflow.workflow, "wait_condition", wait_condition)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(
            workflow_id="chronicler-audio--parent/chat-8",
            run_id="run-8",
        ),
    )
    instance = _workflow(package)
    instance._operations = Operations()
    running = asyncio.create_task(
        instance.run(AudioGenerationRequest(package=package, mode="normal"))
    )
    await asyncio.sleep(0)
    for _ in range(100):
        proposal = instance.audio_status().pending_destination_revision
        if proposal is not None:
            break
        await asyncio.sleep(0)
    instance.approve_destination(
        AudioDestinationApproval(
            revision=proposal,
            bridge_id=package.bridge_id,
            root_id=package.root_id,
            folder_binding_id=package.folder_binding_id,
        )
    )

    await running
    receipts = instance.audio_status().receipts

    assert [(receipt.artifact_role, receipt.relative_path) for receipt in receipts] == [
        ("wav", package.wav_path),
        ("synthetic_transcript", proposal.synthetic_markdown_path),
    ]
    assert proposal.wav_path == package.wav_path
    assert [request.arguments["artifact_role"] for request in requests] == [
        "wav",
        "synthetic_transcript",
        "synthetic_transcript",
    ]


@pytest.mark.asyncio
async def test_unrecoverable_write_failure_returns_recovery_needed(monkeypatch) -> None:
    package = _package()
    synthesized = await _stub_synthesize_approved_audio(package)

    class Operations:
        async def run(self, request, result_type):
            raise LocalOperationFailed(
                LocalOperationOutcome(
                    operation_id=request.operation_id,
                    status=LocalOperationStatus.FAILED,
                    error="folder permission was revoked",
                )
            )

    async def execute_activity(*args, **kwargs):
        return synthesized

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(
            workflow_id="chronicler-audio--parent/chat-7",
            run_id="run-7",
        ),
    )
    instance = _workflow(package)
    instance._operations = Operations()

    result = await instance.run(AudioGenerationRequest(package=package, mode="normal"))

    assert result.outcome == "needs_recovery"
    assert result.status.phase == "failed"
    assert result.duration_s == synthesized.duration_s
    assert instance.audio_status().state == "failed"
    assert instance._package == package


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_local_write_cancels_pending_operations(
    monkeypatch,
) -> None:
    package = _package()
    synthesized = await _stub_synthesize_approved_audio(package)
    waiting = asyncio.Event()
    canceled_reasons = []
    cleanup_phases = []

    class Operations:
        async def run(self, request, result_type):
            waiting.set()
            await asyncio.Event().wait()

        def cancel_pending(self, *, reason):
            cleanup_phases.append(instance.audio_status().status.phase)
            canceled_reasons.append(reason)
            return ["pending-write"]

    async def execute_activity(*args, **kwargs):
        return synthesized

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(
            workflow_id="chronicler-audio--parent/chat-7",
            run_id="run-7",
        ),
    )
    instance = _workflow(package)
    instance._operations = Operations()
    running = asyncio.create_task(
        instance.run(AudioGenerationRequest(package=package, mode="normal"))
    )
    await waiting.wait()
    assert instance.audio_status().status.phase == "saving_wav"

    running.cancel()
    for _ in range(3):
        await asyncio.sleep(0)
        if instance.audio_status().status.phase == "canceling":
            break
    assert instance.audio_status().status.phase == "canceling"
    result = await running

    assert result.outcome == "canceled"
    assert result.status.phase == "canceled"
    assert canceled_reasons == ["audio workflow canceled"]
    assert cleanup_phases == ["canceling"]


def test_worker_registers_the_audio_child_and_synthesis_activity() -> None:
    from examples.chronicler.worker import CHRONICLER_ACTIVITIES, CHRONICLER_WORKFLOWS
    from examples.chronicler.conversational_workflow import ChroniclerAgentWorkflow

    assert CHRONICLER_WORKFLOWS == (ChroniclerAgentWorkflow, ChroniclerAudioWorkflow)
    assert CHRONICLER_ACTIVITIES == (synthesize_approved_audio,)


def test_audio_child_exposes_the_canonical_local_operation_protocol() -> None:
    definition = audio_workflow.workflow._Definition.must_from_class(
        ChroniclerAudioWorkflow
    )

    assert PENDING_LOCAL_OPERATIONS_QUERY in definition.queries
    assert COMPLETE_LOCAL_OPERATION_UPDATE in definition.updates
    assert definition.updates[COMPLETE_LOCAL_OPERATION_UPDATE].validator is (
        ChroniclerAudioWorkflow.validate_complete_local_operation
    )


@pytest.mark.asyncio
async def test_fixed_id_allows_only_one_active_audio_child() -> None:
    environment = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"audio-child-{uuid.uuid4()}"
    workflow_id = "chronicler-audio--parent/chat-7"
    request = AudioGenerationRequest(package=_package(), mode="normal")
    try:
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ChroniclerAudioWorkflow],
            workflow_failure_exception_types=[Exception],
        ):
            first = await environment.client.start_workflow(
                ChroniclerAudioWorkflow.run,
                request,
                id=workflow_id,
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
            assert (await first.describe()).status is WorkflowExecutionStatus.RUNNING
            with pytest.raises(WorkflowAlreadyStartedError):
                await environment.client.start_workflow(
                    ChroniclerAudioWorkflow.run,
                    request,
                    id=workflow_id,
                    task_queue=task_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                )
            await first.cancel()
    finally:
        await environment.shutdown()


@pytest.mark.asyncio
async def test_fixed_id_can_start_again_after_the_previous_child_closes() -> None:
    _stub_synthesis_calls.clear()
    environment = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"audio-child-reuse-{uuid.uuid4()}"
    workflow_id = "chronicler-audio--parent/chat-7"
    request = AudioGenerationRequest(package=_package(), mode="normal")
    try:
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ChroniclerAudioWorkflow],
            activities=[_stub_synthesize_approved_audio],
            workflow_failure_exception_types=[Exception],
        ):
            first_handle = await environment.client.start_workflow(
                ChroniclerAudioWorkflow.run,
                request,
                id=workflow_id,
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
            first_operation = await _await_pending_operation(first_handle)
            first_receipt = await _complete_artifact_operation(
                first_handle, first_operation
            )
            first = await first_handle.result()
            second_handle = await environment.client.start_workflow(
                ChroniclerAudioWorkflow.run,
                AudioGenerationRequest(package=_package(), mode="recovery"),
                id=workflow_id,
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
            second_operation = await _await_pending_operation(second_handle)
            await second_handle.execute_update(
                COMPLETE_LOCAL_OPERATION_UPDATE,
                LocalOperationResult(
                    operation_id=second_operation.operation_id,
                    result=AudioArtifactInspectionResult(
                        status="owned",
                        receipt=first_receipt,
                        duration_s=first.duration_s,
                    ),
                ),
            )
            second = await second_handle.result()
    finally:
        await environment.shutdown()

    assert first.generation_id == second.generation_id == _package().generation_id
    assert _stub_synthesis_calls == [_package().generation_id]
    assert second_operation.kind == "inspect_audio_artifact"
    assert second_operation.arguments["approved_package_revision"] == 1


@pytest.mark.asyncio
async def test_temporal_recovery_writes_only_missing_markdown() -> None:
    _stub_synthesis_calls.clear()
    package = _synthetic_package()
    environment = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"audio-child-recovery-{uuid.uuid4()}"
    workflow_id = "chronicler-audio--parent/chat-8"
    try:
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[ChroniclerAudioWorkflow],
            activities=[_stub_synthesize_approved_audio],
            workflow_failure_exception_types=[Exception],
        ):
            first_handle = await environment.client.start_workflow(
                ChroniclerAudioWorkflow.run,
                AudioGenerationRequest(package=package, mode="normal"),
                id=workflow_id,
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
            first_wav = await _await_pending_operation(first_handle)
            wav_receipt = await _complete_artifact_operation(first_handle, first_wav)
            first_markdown = await _await_pending_operation(first_handle)
            await _complete_artifact_operation(first_handle, first_markdown)
            await first_handle.result()

            recovery_handle = await environment.client.start_workflow(
                ChroniclerAudioWorkflow.run,
                AudioGenerationRequest(package=package, mode="recovery"),
                id=workflow_id,
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
            inspect_wav = await _await_pending_operation(recovery_handle)
            await recovery_handle.execute_update(
                COMPLETE_LOCAL_OPERATION_UPDATE,
                LocalOperationResult(
                    operation_id=inspect_wav.operation_id,
                    result=AudioArtifactInspectionResult(
                        status="owned", receipt=wav_receipt, duration_s=1.0
                    ),
                ),
            )
            inspect_markdown = await _await_pending_operation(recovery_handle)
            await recovery_handle.execute_update(
                COMPLETE_LOCAL_OPERATION_UPDATE,
                LocalOperationResult(
                    operation_id=inspect_markdown.operation_id,
                    result=AudioArtifactInspectionResult(status="missing"),
                ),
            )
            recovery_create = await _await_pending_operation(recovery_handle)
            await _complete_artifact_operation(recovery_handle, recovery_create)
            recovered = await recovery_handle.result()
    finally:
        await environment.shutdown()

    assert recovered.outcome == "completed"
    assert _stub_synthesis_calls == [package.generation_id]
    assert [inspect_wav.kind, inspect_markdown.kind, recovery_create.kind] == [
        "inspect_audio_artifact",
        "inspect_audio_artifact",
        "create_audio_artifact",
    ]
    assert recovery_create.arguments["artifact_role"] == "synthetic_transcript"
    assert recovery_create.operation_id == first_markdown.operation_id


def test_destination_approval_rejects_a_mismatched_folder_binding() -> None:
    package = _package()
    instance = _workflow()
    instance._package = package
    instance._result = None
    instance._status = AudioGenerationStatus(
        generation_id=package.generation_id,
        child_workflow_id="chronicler-audio--parent/chat-7",
        phase="destination_approval_needed",
    )
    approval = AudioDestinationApproval(
        revision=DestinationRevision(
            generation_id=package.generation_id,
            content_digest=package.content_digest,
            destination_revision=2,
            wav_path="audio/session-7-recap-v2.wav",
        ),
        bridge_id=package.bridge_id,
        root_id=package.root_id,
        folder_binding_id="different-binding",
    )

    with pytest.raises(ApplicationError) as failure:
        instance.approve_destination(approval)

    assert failure.value.type == "AudioBindingMismatch"


def test_destination_approval_rejects_the_generating_audio_phase() -> None:
    package = _package()
    instance = _workflow(package)
    instance._package = package
    instance._result = None
    instance._status = AudioGenerationStatus(
        generation_id=package.generation_id,
        child_workflow_id="chronicler-audio--parent/chat-7",
        phase="generating_audio",
    )
    approval = AudioDestinationApproval(
        revision=DestinationRevision(
            generation_id=package.generation_id,
            content_digest=package.content_digest,
            destination_revision=2,
            wav_path="audio/session-7-recap-v2.wav",
        ),
        bridge_id=package.bridge_id,
        root_id=package.root_id,
        folder_binding_id=package.folder_binding_id,
    )

    with pytest.raises(ApplicationError) as failure:
        instance.approve_destination(approval)

    assert failure.value.type == "AudioDestinationPhaseMismatch"


@pytest.mark.parametrize(
    "phase",
    ["saving_wav", "waiting_for_browser", "waiting_for_folder"],
)
def test_destination_approval_rejects_every_other_nonterminal_phase(
    phase: str,
) -> None:
    package = _package()
    instance = _workflow(package)
    instance._package = package
    instance._result = None
    instance._status = AudioGenerationStatus.model_construct(
        generation_id=package.generation_id,
        child_workflow_id="chronicler-audio--parent/chat-7",
        phase=phase,
        detail="",
    )
    approval = AudioDestinationApproval(
        revision=DestinationRevision(
            generation_id=package.generation_id,
            content_digest=package.content_digest,
            destination_revision=2,
            wav_path="audio/session-7-recap-v2.wav",
        ),
        bridge_id=package.bridge_id,
        root_id=package.root_id,
        folder_binding_id=package.folder_binding_id,
    )

    with pytest.raises(ApplicationError) as failure:
        instance.approve_destination(approval)

    assert failure.value.type == "AudioDestinationPhaseMismatch"


def test_destination_approval_rejects_generation_identity_drift() -> None:
    package = _package()
    instance = _workflow(package)
    instance._package = package
    instance._result = None
    instance._status = AudioGenerationStatus(
        generation_id=package.generation_id,
        child_workflow_id="chronicler-audio--parent/chat-7",
        phase="destination_approval_needed",
    )
    approval = AudioDestinationApproval(
        revision=DestinationRevision(
            generation_id="generation-other",
            content_digest=package.content_digest,
            destination_revision=2,
            wav_path="audio/session-7-recap-v2.wav",
        ),
        bridge_id=package.bridge_id,
        root_id=package.root_id,
        folder_binding_id=package.folder_binding_id,
    )

    with pytest.raises(ApplicationError) as failure:
        instance.approve_destination(approval)

    assert failure.value.type == "AudioDestinationIdentityMismatch"


def test_destination_approval_rejects_content_identity_drift() -> None:
    package = _package()
    instance = _workflow(package)
    instance._package = package
    instance._result = None
    instance._status = AudioGenerationStatus(
        generation_id=package.generation_id,
        child_workflow_id="chronicler-audio--parent/chat-7",
        phase="destination_approval_needed",
    )
    approval = AudioDestinationApproval(
        revision=DestinationRevision(
            generation_id=package.generation_id,
            content_digest="d" * 64,
            destination_revision=2,
            wav_path="audio/session-7-recap-v2.wav",
        ),
        bridge_id=package.bridge_id,
        root_id=package.root_id,
        folder_binding_id=package.folder_binding_id,
    )

    with pytest.raises(ApplicationError) as failure:
        instance.approve_destination(approval)

    assert failure.value.type == "AudioDestinationIdentityMismatch"


def test_destination_approval_rejects_a_nonsequential_revision() -> None:
    package = _package()
    instance = _workflow(package)
    instance._package = package
    instance._result = None
    instance._status = AudioGenerationStatus(
        generation_id=package.generation_id,
        child_workflow_id="chronicler-audio--parent/chat-7",
        phase="destination_approval_needed",
    )
    approval = AudioDestinationApproval(
        revision=DestinationRevision(
            generation_id=package.generation_id,
            content_digest=package.content_digest,
            destination_revision=3,
            wav_path="audio/session-7-recap-v3.wav",
        ),
        bridge_id=package.bridge_id,
        root_id=package.root_id,
        folder_binding_id=package.folder_binding_id,
    )

    with pytest.raises(ApplicationError) as failure:
        instance.approve_destination(approval)

    assert failure.value.type == "AudioDestinationRevisionMismatch"


def test_destination_approval_applies_the_next_revision_authoritatively() -> None:
    package = _package()
    instance = _workflow(package)
    instance._package = package
    instance._result = None
    instance._status = AudioGenerationStatus(
        generation_id=package.generation_id,
        child_workflow_id="chronicler-audio--parent/chat-7",
        phase="destination_approval_needed",
    )
    revision = DestinationRevision(
        generation_id=package.generation_id,
        content_digest=package.content_digest,
        destination_revision=2,
        wav_path="audio/session-7-recap-v2.wav",
    )
    instance._pending_destination_revision = revision

    snapshot = instance.approve_destination(
        AudioDestinationApproval(
            revision=revision,
            bridge_id=package.bridge_id,
            root_id=package.root_id,
            folder_binding_id=package.folder_binding_id,
        )
    )

    assert instance._package.package_revision == revision.destination_revision
    assert instance._package.wav_path == revision.wav_path
    assert instance._package.generation_id == package.generation_id
    assert instance._package.content_digest == package.content_digest
    assert instance._package.destination_digest == revision.destination_digest
    assert instance._package.package_digest == revision.package_digest
    assert snapshot.status.phase == "saving_wav"


def test_destination_approval_rejects_paths_other_than_the_pending_proposal() -> None:
    package = _package()
    instance = _workflow(package)
    instance._package = package
    instance._result = None
    instance._status = AudioGenerationStatus(
        generation_id=package.generation_id,
        child_workflow_id="chronicler-audio--parent/chat-7",
        phase="destination_approval_needed",
    )
    instance._pending_destination_revision = DestinationRevision(
        generation_id=package.generation_id,
        content_digest=package.content_digest,
        destination_revision=2,
        wav_path="audio/session-7-recap-2.wav",
    )
    different = DestinationRevision(
        generation_id=package.generation_id,
        content_digest=package.content_digest,
        destination_revision=2,
        wav_path="audio/different.wav",
    )

    with pytest.raises(ApplicationError) as failure:
        instance.approve_destination(
            AudioDestinationApproval(
                revision=different,
                bridge_id=package.bridge_id,
                root_id=package.root_id,
                folder_binding_id=package.folder_binding_id,
            )
        )

    assert failure.value.type == "AudioDestinationProposalMismatch"


@pytest.mark.asyncio
async def test_child_records_authoritative_canceled_status(monkeypatch) -> None:
    async def canceled_activity(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(audio_workflow.workflow, "execute_activity", canceled_activity)
    monkeypatch.setattr(
        audio_workflow.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="chronicler-audio--parent/chat-7"),
    )
    instance = _workflow()

    result = await instance.run(
        AudioGenerationRequest(package=_package(), mode="normal")
    )

    assert result.outcome == "canceled"
    assert result.status.phase == "canceled"
    assert instance.audio_status().state == "canceled"


def test_destination_approval_rejects_an_already_terminal_generation() -> None:
    package = _package()
    terminal_status = AudioGenerationStatus(
        generation_id=package.generation_id,
        child_workflow_id="chronicler-audio--parent/chat-7",
        phase="complete",
    )
    instance = _workflow(package)
    instance._package = package
    instance._status = terminal_status
    instance._result = AudioGenerationResult(
        generation_id=package.generation_id,
        outcome="completed",
        status=terminal_status,
    )
    approval = AudioDestinationApproval(
        revision=DestinationRevision(
            generation_id=package.generation_id,
            content_digest=package.content_digest,
            destination_revision=2,
            wav_path="audio/session-7-recap-v2.wav",
        ),
        bridge_id=package.bridge_id,
        root_id=package.root_id,
        folder_binding_id=package.folder_binding_id,
    )

    with pytest.raises(ApplicationError) as failure:
        instance.approve_destination(approval)

    assert failure.value.type == "AudioDestinationAlreadySettled"
