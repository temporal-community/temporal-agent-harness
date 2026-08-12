"""Model-free child workflow for an approved Chronicler audio package."""

import asyncio
import base64
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Literal

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.local_operations import (
        COMPLETE_LOCAL_OPERATION_UPDATE,
        PENDING_LOCAL_OPERATIONS_QUERY,
        LocalOperationAck,
        LocalOperationFailed,
        LocalOperationOutcome,
        LocalOperationRequest,
        LocalOperationResult,
        LocalOperationStatus,
        LocalOperations,
    )

    from .audio_activities import synthesize_approved_audio
    from .audio_models import (
        AudioDestinationApproval,
        AudioApprovalPackage,
        AudioArtifactInspectionResult,
        AudioGenerationRequest,
        AudioGenerationResult,
        AudioGenerationSnapshot,
        AudioGenerationStatus,
        ArtifactReceipt,
        CreateAudioArtifactResult,
        DestinationRevision,
        SynthesizedWav,
    )

AUDIO_STATUS_QUERY = "audio_generation_status"
APPROVE_AUDIO_DESTINATION_UPDATE = "approve_audio_destination"


@workflow.defn(name="ChroniclerAudioWorkflow")
class ChroniclerAudioWorkflow:
    @workflow.init
    def __init__(self, request: AudioGenerationRequest) -> None:
        self._operations = LocalOperations()
        self._receipts = []
        self._pending_destination_revision = None
        self._synthesized_wav = None
        self._package = request.package

    @workflow.query(name=PENDING_LOCAL_OPERATIONS_QUERY)
    def pending_local_operations(self) -> list[LocalOperationRequest]:
        return self._operations.pending()

    @workflow.update(name=COMPLETE_LOCAL_OPERATION_UPDATE)
    async def complete_local_operation(
        self, result: LocalOperationResult
    ) -> LocalOperationAck:
        return self._operations.complete(result)

    @complete_local_operation.validator
    def validate_complete_local_operation(self, result: LocalOperationResult) -> None:
        self._operations.validate_completion(result)

    @workflow.query(name=AUDIO_STATUS_QUERY)
    def audio_status(self) -> AudioGenerationSnapshot:
        state = "running"
        if self._result is not None:
            state = (
                "failed"
                if self._result.outcome == "needs_recovery"
                else self._result.outcome
            )
        return AudioGenerationSnapshot(
            child_workflow_id=self._status.child_workflow_id,
            state=state,
            status=self._status,
            approved_package=self._package,
            result=self._result,
            receipts=tuple(self._receipts),
            pending_destination_revision=self._pending_destination_revision,
        )

    @workflow.update(name=APPROVE_AUDIO_DESTINATION_UPDATE)
    def approve_destination(
        self, approval: AudioDestinationApproval
    ) -> AudioGenerationSnapshot:
        package = self._package
        if self._result is not None:
            raise ApplicationError(
                "audio generation is already terminal",
                type="AudioDestinationAlreadySettled",
                non_retryable=True,
            )
        if self._status.phase != "destination_approval_needed":
            raise ApplicationError(
                "audio generation is not awaiting destination approval",
                type="AudioDestinationPhaseMismatch",
                non_retryable=True,
            )
        if (
            approval.bridge_id,
            approval.root_id,
            approval.folder_binding_id,
        ) != (package.bridge_id, package.root_id, package.folder_binding_id):
            raise ApplicationError(
                "destination approval binding does not match the audio package",
                type="AudioBindingMismatch",
                non_retryable=True,
            )
        if (
            approval.revision.generation_id,
            approval.revision.content_digest,
        ) != (package.generation_id, package.content_digest):
            raise ApplicationError(
                "destination approval identity does not match the audio package",
                type="AudioDestinationIdentityMismatch",
                non_retryable=True,
            )
        if approval.revision.destination_revision != package.package_revision + 1:
            raise ApplicationError(
                "destination approval must be the next package revision",
                type="AudioDestinationRevisionMismatch",
                non_retryable=True,
            )
        if approval.revision != self._pending_destination_revision:
            raise ApplicationError(
                "destination approval must exactly match the pending proposal",
                type="AudioDestinationProposalMismatch",
                non_retryable=True,
            )
        revision = approval.revision
        self._package = package.model_copy(
            update={
                "package_revision": revision.destination_revision,
                "wav_path": revision.wav_path,
                "synthetic_markdown_path": revision.synthetic_markdown_path,
            }
        )
        self._pending_destination_revision = None
        self._status = self._status.model_copy(update={"phase": "saving_wav"})
        return self.audio_status()

    @workflow.run
    async def run(self, request: AudioGenerationRequest) -> AudioGenerationResult:
        package = request.package
        self._package = package
        self._result: AudioGenerationResult | None = None
        self._receipts: list[ArtifactReceipt] = []
        self._pending_destination_revision = None
        self._status = AudioGenerationStatus(
            generation_id=package.generation_id,
            child_workflow_id=workflow.info().workflow_id,
            phase="generating_audio",
        )
        duration_s: float | None = None
        try:
            roles_to_write = list(self._required_roles(package))
            if request.mode == "recovery":
                roles_to_write, duration_s = await self._inspect_recovery(package)
            if "wav" in roles_to_write:
                self._status = self._status.model_copy(
                    update={"phase": "generating_audio"}
                )
                try:
                    self._synthesized_wav = await workflow.execute_activity(
                        synthesize_approved_audio,
                        package,
                        start_to_close_timeout=timedelta(minutes=3),
                    )
                except Exception as error:
                    self._status = self._status.model_copy(
                        update={
                            "phase": "failed",
                            "detail": f"audio synthesis failed: {error}",
                        }
                    )
                    self._result = AudioGenerationResult(
                        generation_id=package.generation_id,
                        outcome="failed",
                        status=self._status,
                        approved_package=self._package,
                    )
                    return self._result
                duration_s = self._synthesized_wav.duration_s
            if roles_to_write:
                while True:
                    try:
                        await self._persist_artifacts(
                            self._package,
                            self._synthesized_wav,
                            roles=roles_to_write,
                        )
                        break
                    except LocalOperationFailed as error:
                        if not (error.outcome.error or "").startswith(
                            "audio_artifact_collision:"
                        ):
                            raise
                        collision_role: Literal["wav", "synthetic_transcript"] = (
                            "synthetic_transcript"
                            if error.outcome.operation_id.endswith(
                                ":synthetic_transcript"
                            )
                            else "wav"
                        )
                        self._pending_destination_revision = (
                            self._next_destination_revision(
                                self._package, collision_role=collision_role
                            )
                        )
                        self._status = self._status.model_copy(
                            update={"phase": "destination_approval_needed"}
                        )
                        await workflow.wait_condition(
                            lambda: self._pending_destination_revision is None
                        )
                        roles_to_write = (
                            ["synthetic_transcript"]
                            if collision_role == "synthetic_transcript"
                            else list(self._required_roles(self._package))
                        )
        except asyncio.CancelledError:
            self._status = self._status.model_copy(update={"phase": "canceling"})
            await asyncio.sleep(0)
            self._operations.cancel_pending(reason="audio workflow canceled")
            self._status = self._status.model_copy(update={"phase": "canceled"})
            self._result = AudioGenerationResult(
                generation_id=package.generation_id,
                outcome="canceled",
                status=self._status,
                approved_package=self._package,
            )
            return self._result
        except LocalOperationFailed as error:
            self._status = self._status.model_copy(
                update={"phase": "failed", "detail": str(error)}
            )
            self._result = AudioGenerationResult(
                generation_id=package.generation_id,
                outcome="needs_recovery",
                status=self._status,
                duration_s=duration_s,
                approved_package=self._package,
            )
            return self._result
        self._status = AudioGenerationStatus(
            generation_id=package.generation_id,
            child_workflow_id=workflow.info().workflow_id,
            phase="complete",
        )
        self._result = AudioGenerationResult(
            generation_id=package.generation_id,
            outcome="completed",
            status=self._status,
            duration_s=duration_s,
            approved_package=self._package,
        )
        return self._result

    async def _inspect_recovery(
        self, package: AudioApprovalPackage
    ) -> tuple[list[Literal["wav", "synthetic_transcript"]], float | None]:
        duration_s: float | None = None
        missing: list[Literal["wav", "synthetic_transcript"]] = []
        for role in self._required_roles(package):
            self._status = self._status.model_copy(
                update={"phase": "waiting_for_folder"}
            )
            operation = self._inspection_operation(package=package, role=role)
            inspected = await self._operations.run(
                operation,
                AudioArtifactInspectionResult,
            )
            if inspected.status == "missing":
                missing.append(role)
                continue
            self._validate_owned_receipt(
                package=package,
                role=role,
                operation_id=operation.operation_id,
                receipt=inspected.receipt,
            )
            self._record_receipt(inspected.receipt)
            if role == "wav":
                duration_s = inspected.duration_s
        return missing, duration_s

    @staticmethod
    def _validate_owned_receipt(
        *,
        package: AudioApprovalPackage,
        role: Literal["wav", "synthetic_transcript"],
        operation_id: str,
        receipt: ArtifactReceipt,
    ) -> None:
        expected_path = (
            package.wav_path
            if role == "wav"
            else package.synthetic_markdown_path
        )
        expected_receipt_write_id = (
            f"audio-write:{package.generation_id}:"
            f"r{receipt.package_revision}:{role}"
        )
        if (
            receipt.generation_id,
            receipt.artifact_role,
            receipt.relative_path,
            receipt.operation_id,
            receipt.folder_binding_id,
        ) != (
            package.generation_id,
            role,
            expected_path,
            expected_receipt_write_id,
            package.folder_binding_id,
        ) or receipt.package_revision > package.package_revision:
            raise LocalOperationFailed(
                LocalOperationOutcome(
                    operation_id=operation_id,
                    status=LocalOperationStatus.FAILED,
                    error="artifact inspection returned mismatched ownership",
                )
            )

    @staticmethod
    def _required_roles(
        package: AudioApprovalPackage,
    ) -> tuple[Literal["wav", "synthetic_transcript"], ...]:
        if package.synthetic_markdown_path is None:
            return ("wav",)
        return ("wav", "synthetic_transcript")

    @staticmethod
    def _inspection_operation(
        *,
        package: AudioApprovalPackage,
        role: Literal["wav", "synthetic_transcript"],
    ) -> LocalOperationRequest:
        relative_path = (
            package.wav_path
            if role == "wav"
            else package.synthetic_markdown_path
        )
        operation_id = (
            f"audio-inspect:{package.generation_id}:"
            f"r{package.package_revision}:{role}"
        )
        return LocalOperationRequest(
            operation_id=operation_id,
            bridge_id=package.bridge_id,
            root_id=package.root_id,
            kind="inspect_audio_artifact",
            arguments={
                "generation_id": package.generation_id,
                "artifact_role": role,
                "relative_path": relative_path,
                "folder_binding_id": package.folder_binding_id,
                "approved_package_revision": package.package_revision,
            },
            idempotency_key=operation_id,
        )

    async def _persist_artifacts(
        self,
        package: AudioApprovalPackage,
        synthesized: SynthesizedWav | None,
        *,
        roles: list[Literal["wav", "synthetic_transcript"]],
    ) -> None:
        for role in roles:
            phase = "saving_wav" if role == "wav" else "saving_synthetic_transcript"
            self._status = self._status.model_copy(update={"phase": phase})
            operation = self._artifact_operation(
                package=package,
                synthesized=synthesized,
                role=role,
            )
            self._status = self._status.model_copy(
                update={"detail": "awaiting browser artifact fulfillment"}
            )
            created = await self._operations.run(
                operation, CreateAudioArtifactResult
            )
            self._status = self._status.model_copy(update={"detail": ""})
            self._validate_created_result(
                package=package,
                role=role,
                operation=operation,
                created=created,
            )
            self._record_receipt(created.receipt)

    @staticmethod
    def _validate_created_result(
        *,
        package: AudioApprovalPackage,
        role: Literal["wav", "synthetic_transcript"],
        operation: LocalOperationRequest,
        created: CreateAudioArtifactResult,
    ) -> None:
        arguments = operation.arguments
        receipt = created.receipt
        expected = (
            package.generation_id,
            role,
            arguments["relative_path"],
            arguments["expected_content_hash"],
            arguments["expected_content_size"],
            package.package_revision,
            operation.operation_id,
            package.folder_binding_id,
        )
        observed = (
            receipt.generation_id,
            receipt.artifact_role,
            receipt.relative_path,
            receipt.content_hash,
            receipt.content_size,
            receipt.package_revision,
            receipt.operation_id,
            receipt.folder_binding_id,
        )
        if observed != expected:
            raise LocalOperationFailed(
                LocalOperationOutcome(
                    operation_id=operation.operation_id,
                    status=LocalOperationStatus.FAILED,
                    error="artifact creation returned mismatched ownership",
                )
            )

    def _record_receipt(self, receipt: ArtifactReceipt) -> None:
        for index, existing in enumerate(self._receipts):
            if existing.artifact_role == receipt.artifact_role:
                self._receipts[index] = receipt
                return
        self._receipts.append(receipt)

    @staticmethod
    def _next_destination_revision(
        package: AudioApprovalPackage,
        *,
        collision_role: Literal["wav", "synthetic_transcript"],
    ) -> DestinationRevision:
        revision = package.package_revision + 1

        def suffixed(path: str) -> str:
            candidate = PurePosixPath(path)
            return str(
                candidate.with_name(f"{candidate.stem}-{revision}{candidate.suffix}")
            )

        return DestinationRevision(
            generation_id=package.generation_id,
            content_digest=package.content_digest,
            destination_revision=revision,
            wav_path=(
                package.wav_path
                if collision_role == "synthetic_transcript"
                else suffixed(package.wav_path)
            ),
            synthetic_markdown_path=(
                suffixed(package.synthetic_markdown_path)
                if package.synthetic_markdown_path is not None
                else None
            ),
        )

    @staticmethod
    def _artifact_operation(
        *,
        package: AudioApprovalPackage,
        synthesized: SynthesizedWav | None,
        role: Literal["wav", "synthetic_transcript"],
    ) -> LocalOperationRequest:
        operation_id = (
            f"audio-write:{package.generation_id}:"
            f"r{package.package_revision}:{role}"
        )
        if role == "wav":
            if synthesized is None:
                raise ValueError("WAV creation requires synthesized audio")
            relative_path = package.wav_path
            content_base64 = synthesized.audio_base64
            content_hash = synthesized.wav_hash
            content_size = synthesized.wav_size
        else:
            source_bytes = package.source_content.encode("utf-8")
            relative_path = package.synthetic_markdown_path
            content_base64 = base64.b64encode(source_bytes).decode("ascii")
            content_hash = package.source_hash
            content_size = len(source_bytes)
        return LocalOperationRequest(
            operation_id=operation_id,
            bridge_id=package.bridge_id,
            root_id=package.root_id,
            kind="create_audio_artifact",
            arguments={
                "operation_id": operation_id,
                "generation_id": package.generation_id,
                "artifact_role": role,
                "relative_path": relative_path,
                "content_base64": content_base64,
                "expected_content_hash": content_hash,
                "expected_content_size": content_size,
                "folder_binding_id": package.folder_binding_id,
                "package_revision": package.package_revision,
            },
            idempotency_key=(
                f"chronicler-audio:{package.generation_id}:"
                f"r{package.package_revision}:{role}"
            ),
        )
