"""Immutable contracts for Chronicler's approved audio-generation path."""

import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal, Mapping, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

RECAP_TARGET_MIN_SECONDS = 60
RECAP_TARGET_MAX_SECONDS = 90


def _canonical_digest(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_safe_relative_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return bool(path) and "\\" not in path and not candidate.is_absolute() and all(
        part not in {"", ".", ".."} for part in candidate.parts
    )


def _canonical_destination_digest(
    *,
    generation_id: str,
    destination_revision: int,
    wav_path: str,
    synthetic_markdown_path: str | None,
) -> str:
    return _canonical_digest(
        {
            "destination_revision": destination_revision,
            "generation_id": generation_id,
            "synthetic_markdown_path": synthetic_markdown_path,
            "wav_path": wav_path,
        }
    )


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @model_validator(mode="after")
    def validate_identifiers(self) -> Self:
        for field_name in type(self).model_fields:
            if not field_name.endswith(("_id", "_identity")):
                continue
            value = getattr(self, field_name)
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        return self

    def _validated_copy_without(
        self,
        derived_fields: tuple[str, ...],
        update: Mapping[str, object] | None,
    ) -> Self:
        values = self.model_dump()
        for field_name in derived_fields:
            values.pop(field_name)
        values.update(update or {})
        return type(self).model_validate(values)


class ExistingTranscriptSource(_StrictModel):
    """A transcript the browser already read and validated from the archive."""

    source_kind: Literal["existing"] = "existing"
    source_identity: str
    source_content: str
    source_hash: str

    @model_validator(mode="after")
    def validate_content_hash(self) -> Self:
        expected_hash = hashlib.sha256(self.source_content.encode("utf-8")).hexdigest()
        if self.source_hash != expected_hash:
            raise ValueError("source hash must match UTF-8 source content")
        return self


class SyntheticTopicSource(_StrictModel):
    """A topic from which the parent may draft a clearly synthetic transcript."""

    source_kind: Literal["synthetic"] = "synthetic"
    topic: str


class PrepareAudioRequest(_StrictModel):
    """Request a reviewable draft from a validated existing transcript."""

    source: ExistingTranscriptSource | SyntheticTopicSource | None = None
    change_request: str | None = None
    base_draft_digest: str | None = None
    bridge_id: str
    root_id: str
    folder_binding_id: str

    @model_validator(mode="after")
    def validate_change_context(self) -> Self:
        if (self.change_request is None) != (self.base_draft_digest is None):
            raise ValueError(
                "change_request and base_draft_digest must be supplied together"
            )
        if self.change_request is not None and not self.change_request.strip():
            raise ValueError("change_request must not be blank")
        if self.change_request is None and self.source is None:
            raise ValueError("initial preparation requires a source")
        return self


class AudioDraft(_StrictModel):
    """The complete review package before a generation identity exists."""

    draft_id: str
    source_kind: Literal["existing", "synthetic"]
    source_identity: str
    source_content: str
    source_hash: str
    recap_script: str
    voice: Literal["Charon"] = "Charon"
    wav_path: str
    synthetic_markdown_path: str | None = None
    bridge_id: str
    root_id: str
    folder_binding_id: str
    draft_digest: str | None = None

    @model_validator(mode="after")
    def validate_draft_digest(self) -> Self:
        expected_digest = _canonical_digest(
            {
                "bridge_id": self.bridge_id,
                "folder_binding_id": self.folder_binding_id,
                "recap_script": self.recap_script,
                "root_id": self.root_id,
                "source_content": self.source_content,
                "source_hash": self.source_hash,
                "source_identity": self.source_identity,
                "source_kind": self.source_kind,
                "synthetic_markdown_path": self.synthetic_markdown_path,
                "voice": self.voice,
                "wav_path": self.wav_path,
            }
        )
        if self.draft_digest is not None and self.draft_digest != expected_digest:
            raise ValueError("draft_digest does not match canonical draft fields")
        object.__setattr__(self, "draft_digest", expected_digest)
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        return self._validated_copy_without(("draft_digest",), update)


class AudioDraftResponse(_StrictModel):
    """The parent returns a draft, never a started generation, from preparation."""

    draft: AudioDraft


class StartAudioRequest(_StrictModel):
    """The explicit full-package approval request after browser preflight."""

    draft_id: str
    draft_digest: str
    bridge_id: str
    root_id: str
    folder_binding_id: str
    preflighted_paths: tuple[str, ...]

    @field_validator("preflighted_paths", mode="before")
    @classmethod
    def normalize_preflighted_paths(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class RecoverAudioRequest(_StrictModel):
    """Explicit authority to restart an unchanged approved package after close."""

    generation_id: str
    content_digest: str
    destination_digest: str
    package_digest: str
    bridge_id: str
    root_id: str
    folder_binding_id: str


class AudioApprovalPackage(_StrictModel):
    """The immutable fields approved before an audio child may start."""

    package_revision: int = Field(gt=0)
    generation_id: str
    source_kind: Literal["existing", "synthetic"]
    source_identity: str
    source_content: str
    source_hash: str
    recap_script: str
    voice: Literal["Charon"] = "Charon"
    wav_path: str
    synthetic_markdown_path: str | None = None
    bridge_id: str
    root_id: str
    folder_binding_id: str
    content_digest: str | None = None
    destination_digest: str | None = None
    package_digest: str | None = None

    @model_validator(mode="after")
    def validate_source_provenance(self) -> Self:
        if not self.source_content.strip() or not self.source_hash.strip():
            raise ValueError("source content and hash are required")
        expected_hash = hashlib.sha256(self.source_content.encode("utf-8")).hexdigest()
        if self.source_hash != expected_hash:
            raise ValueError("source hash must match UTF-8 source content")
        if not self.wav_path.endswith(".wav") or not _is_safe_relative_path(self.wav_path):
            raise ValueError("wav_path must be a safe relative WAV path")
        if self.source_kind == "existing" and self.synthetic_markdown_path is not None:
            raise ValueError("existing sources cannot have a synthetic Markdown path")
        if self.source_kind == "synthetic":
            if not self.source_content.startswith("# Synthetic Transcript\n"):
                raise ValueError("synthetic content must have a Synthetic Transcript heading")
            if (
                not self.synthetic_markdown_path
                or not self.synthetic_markdown_path.endswith(".md")
                or not _is_safe_relative_path(self.synthetic_markdown_path)
            ):
                raise ValueError("synthetic sources require a Markdown destination")
            if (
                PurePosixPath(self.synthetic_markdown_path).parent
                != PurePosixPath(self.wav_path).parent
            ):
                raise ValueError("synthetic Markdown path must be a sibling of the WAV")

        expected_content_digest = _canonical_digest(
            {
                "bridge_id": self.bridge_id,
                "folder_binding_id": self.folder_binding_id,
                "recap_script": self.recap_script,
                "root_id": self.root_id,
                "source_content": self.source_content,
                "source_hash": self.source_hash,
                "source_identity": self.source_identity,
                "source_kind": self.source_kind,
                "voice": self.voice,
            }
        )
        expected_destination_digest = _canonical_destination_digest(
            generation_id=self.generation_id,
            destination_revision=self.package_revision,
            wav_path=self.wav_path,
            synthetic_markdown_path=self.synthetic_markdown_path,
        )
        expected_package_digest = _canonical_digest(
            {
                "content_digest": expected_content_digest,
                "destination_digest": expected_destination_digest,
            }
        )
        expected_digests = {
            "content_digest": expected_content_digest,
            "destination_digest": expected_destination_digest,
            "package_digest": expected_package_digest,
        }
        for field_name, expected_digest in expected_digests.items():
            supplied_digest = getattr(self, field_name)
            if supplied_digest is not None and supplied_digest != expected_digest:
                raise ValueError(f"{field_name} does not match canonical approved fields")
            object.__setattr__(self, field_name, expected_digest)
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        return self._validated_copy_without(
            ("content_digest", "destination_digest", "package_digest"), update
        )


class AudioGenerationRequest(_StrictModel):
    """Durable child-workflow input for an already-approved package."""

    package: AudioApprovalPackage
    mode: Literal["normal", "recovery"]


class AudioGenerationStatus(_StrictModel):
    """Coarse child progress exposed to the parent and browser."""

    generation_id: str
    child_workflow_id: str
    phase: Literal[
        "generating_audio",
        "saving_wav",
        "saving_synthetic_transcript",
        "destination_approval_needed",
        "waiting_for_folder",
        "canceling",
        "complete",
        "failed",
        "canceled",
    ]
    detail: str = ""


class AudioGenerationResult(_StrictModel):
    """Terminal child result; artifact receipts are attached by persistence work."""

    generation_id: str
    outcome: Literal["completed", "failed", "canceled", "needs_recovery"]
    status: AudioGenerationStatus
    duration_s: float | None = Field(default=None, ge=0)
    approved_package: AudioApprovalPackage | None = None

    @model_validator(mode="after")
    def validate_status_generation(self) -> Self:
        if self.status.generation_id != self.generation_id:
            raise ValueError("status generation_id must match result generation_id")
        expected_phase = {
            "completed": "complete",
            "failed": "failed",
            "canceled": "canceled",
            "needs_recovery": "failed",
        }[self.outcome]
        if self.status.phase != expected_phase:
            raise ValueError(
                f"{self.outcome} outcome requires {expected_phase} status phase"
            )
        return self


class DestinationRevision(_StrictModel):
    """A destination-only approval revision that preserves approved content."""

    generation_id: str
    content_digest: str
    destination_revision: int = Field(gt=0)
    wav_path: str
    synthetic_markdown_path: str | None = None
    destination_digest: str | None = None
    package_digest: str | None = None

    @model_validator(mode="after")
    def validate_digests(self) -> Self:
        wav_path = PurePosixPath(self.wav_path)
        if not _is_safe_relative_path(self.wav_path):
            raise ValueError("wav_path must be a safe relative WAV path")
        if wav_path.suffix != ".wav":
            raise ValueError("wav_path must be a WAV path")
        if self.synthetic_markdown_path is not None:
            markdown_path = PurePosixPath(self.synthetic_markdown_path)
            if not _is_safe_relative_path(self.synthetic_markdown_path):
                raise ValueError(
                    "synthetic_markdown_path must be a safe relative Markdown path"
                )
            if markdown_path.suffix != ".md":
                raise ValueError("synthetic_markdown_path must be a Markdown path")
            if markdown_path.parent != wav_path.parent:
                raise ValueError(
                    "synthetic_markdown_path must be a sibling of the WAV"
                )
        expected_destination_digest = _canonical_destination_digest(
            generation_id=self.generation_id,
            destination_revision=self.destination_revision,
            wav_path=self.wav_path,
            synthetic_markdown_path=self.synthetic_markdown_path,
        )
        expected_package_digest = _canonical_digest(
            {
                "content_digest": self.content_digest,
                "destination_digest": expected_destination_digest,
            }
        )
        expected_digests = {
            "destination_digest": expected_destination_digest,
            "package_digest": expected_package_digest,
        }
        for field_name, expected_digest in expected_digests.items():
            supplied_digest = getattr(self, field_name)
            if supplied_digest is not None and supplied_digest != expected_digest:
                raise ValueError(f"{field_name} does not match canonical revision fields")
            object.__setattr__(self, field_name, expected_digest)
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        return self._validated_copy_without(
            ("destination_digest", "package_digest"), update
        )


class AudioDestinationApproval(_StrictModel):
    """Browser-preflight-backed authority for one destination-only revision."""

    revision: DestinationRevision
    bridge_id: str
    root_id: str
    folder_binding_id: str


class ArtifactReceipt(_StrictModel):
    """Browser-owned proof that a create-only artifact belongs to this generation."""

    generation_id: str
    artifact_role: Literal["wav", "synthetic_transcript"]
    relative_path: str
    content_hash: str
    content_size: int = Field(ge=0)
    package_revision: int = Field(gt=0)
    operation_id: str
    folder_binding_id: str


class AudioArtifactInspectionResult(_StrictModel):
    """Browser inspection of one stable, previously issued artifact write."""

    status: Literal["missing", "owned"]
    receipt: ArtifactReceipt | None = None
    duration_s: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_status_payload(self) -> Self:
        if self.status == "missing" and (
            self.receipt is not None or self.duration_s is not None
        ):
            raise ValueError("missing artifact inspection cannot include ownership data")
        if self.status == "owned" and self.receipt is None:
            raise ValueError("owned artifact inspection requires a receipt")
        return self


class CreateAudioArtifactResult(_StrictModel):
    """Validated create-only bridge result with durable ownership proof."""

    status: Literal["created", "reused"]
    relative_path: str
    observed_content_hash: str
    content_size: int = Field(ge=0)
    receipt: ArtifactReceipt

    @model_validator(mode="after")
    def validate_receipt_content(self) -> Self:
        if (
            self.relative_path,
            self.observed_content_hash,
            self.content_size,
        ) != (
            self.receipt.relative_path,
            self.receipt.content_hash,
            self.receipt.content_size,
        ):
            raise ValueError("artifact result must match its ownership receipt")
        return self


class AudioGenerationSnapshot(_StrictModel):
    """Authoritative child state returned by workflow controls and HTTP adapters."""

    child_workflow_id: str
    state: Literal["running", "completed", "canceled", "failed"]
    status: AudioGenerationStatus
    approved_package: AudioApprovalPackage
    result: AudioGenerationResult | None = None
    receipts: tuple[ArtifactReceipt, ...] = ()
    pending_destination_revision: DestinationRevision | None = None

    @model_validator(mode="after")
    def validate_approved_package(self) -> Self:
        if self.approved_package.generation_id != self.status.generation_id:
            raise ValueError("approved package must match snapshot generation")
        if (
            self.result is not None
            and self.result.approved_package is not None
            and self.result.approved_package != self.approved_package
        ):
            raise ValueError("terminal approved package must match result")
        return self


class SynthesizedWav(_StrictModel):
    """Validated WAV bytes and measured metadata for the exact approved script."""

    script: str
    voice: Literal["Charon"]
    audio_base64: str
    wav_hash: str
    wav_size: int
    duration_s: float
    sample_rate_hz: int
    channels: int
    sample_width_bytes: int
