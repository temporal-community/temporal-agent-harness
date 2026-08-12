"""Chronicler-only FastAPI routes for audio-generation child workflows."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from temporalio.client import Client, WorkflowHandle, WorkflowUpdateFailedError
from temporalio.service import RPCError, RPCStatusCode

from .audio_models import (
    AudioDestinationApproval,
    AudioGenerationSnapshot,
    DestinationRevision,
)
from .audio_workflow import (
    APPROVE_AUDIO_DESTINATION_UPDATE,
    AUDIO_STATUS_QUERY,
)

router = APIRouter(prefix="/api/chronicler/audio", tags=["Chronicler audio"])


class AudioDestinationApprovalRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    content_digest: str = Field(min_length=1)
    destination_revision: int = Field(gt=0)
    wav_path: str = Field(min_length=1)
    synthetic_markdown_path: str | None = None
    bridge_id: str = Field(min_length=1)
    root_id: str = Field(min_length=1)
    folder_binding_id: str = Field(min_length=1)

    def to_destination_revision(self) -> DestinationRevision:
        return DestinationRevision(
            generation_id=self.generation_id,
            content_digest=self.content_digest,
            destination_revision=self.destination_revision,
            wav_path=self.wav_path,
            synthetic_markdown_path=self.synthetic_markdown_path,
        )

    @model_validator(mode="after")
    def validate_destination_revision(self) -> AudioDestinationApprovalRequestBody:
        self.to_destination_revision()
        return self


class AudioCancelRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)


_CHRONICLER_AUDIO_WORKFLOW_ID_PREFIX = "chronicler-audio--"


def _require_chronicler_audio_workflow_id(workflow_id: str) -> str:
    if not workflow_id.startswith(_CHRONICLER_AUDIO_WORKFLOW_ID_PREFIX):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_audio_workflow_id",
                "message": (
                    "workflow_id must start with "
                    f"{_CHRONICLER_AUDIO_WORKFLOW_ID_PREFIX!r}"
                ),
            },
        )
    return workflow_id


@router.get(
    "/status",
    summary="Read authoritative Chronicler audio generation state",
)
async def chronicler_audio_status(
    request: Request,
    workflow_id: str = Query(min_length=1),
) -> JSONResponse:
    workflow_id = _require_chronicler_audio_workflow_id(workflow_id)
    snapshot = await _query_audio_snapshot(request.app.state.temporal, workflow_id)
    return _snapshot_response(snapshot)


@router.post(
    "/destination",
    summary="Approve a Chronicler audio destination revision",
)
async def chronicler_audio_destination(
    request: Request,
    req: AudioDestinationApprovalRequestBody,
) -> JSONResponse:
    workflow_id = _require_chronicler_audio_workflow_id(req.workflow_id)
    snapshot = await _approve_audio_destination(
        request.app.state.temporal,
        workflow_id,
        AudioDestinationApproval(
            revision=req.to_destination_revision(),
            bridge_id=req.bridge_id,
            root_id=req.root_id,
            folder_binding_id=req.folder_binding_id,
        ),
    )
    return _snapshot_response(snapshot)


@router.post(
    "/cancel",
    summary="Cancel a Chronicler audio generation",
)
async def chronicler_audio_cancel(
    request: Request,
    req: AudioCancelRequestBody,
) -> JSONResponse:
    workflow_id = _require_chronicler_audio_workflow_id(req.workflow_id)
    snapshot = await _cancel_audio_generation(
        request.app.state.temporal,
        workflow_id,
    )
    return _snapshot_response(snapshot)


def _snapshot_response(snapshot: AudioGenerationSnapshot) -> JSONResponse:
    return JSONResponse(
        content=snapshot.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


async def _query_audio_snapshot(
    temporal: Client,
    workflow_id: str,
) -> AudioGenerationSnapshot:
    handle = temporal.get_workflow_handle(workflow_id)
    return await _query_audio_snapshot_from_handle(handle, workflow_id)


async def _query_audio_snapshot_from_handle(
    handle: WorkflowHandle[Any, Any],
    workflow_id: str,
) -> AudioGenerationSnapshot:
    try:
        return await handle.query(
            AUDIO_STATUS_QUERY,
            result_type=AudioGenerationSnapshot,
        )
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        raise HTTPException(
            status_code=404,
            detail=f"Audio workflow {workflow_id!r} was not found.",
        ) from exc


async def _approve_audio_destination(
    temporal: Client,
    workflow_id: str,
    approval: AudioDestinationApproval,
) -> AudioGenerationSnapshot:
    handle = temporal.get_workflow_handle(workflow_id)
    try:
        return await handle.execute_update(
            APPROVE_AUDIO_DESTINATION_UPDATE,
            approval,
            result_type=AudioGenerationSnapshot,
        )
    except WorkflowUpdateFailedError as exc:
        cause = exc.cause
        error_type = getattr(cause, "type", None) or type(cause).__name__
        error_name = {
            "AudioBindingMismatch": "audio_binding_mismatch",
            "AudioDestinationPhaseMismatch": "audio_destination_phase_mismatch",
            "AudioDestinationIdentityMismatch": "audio_destination_identity_mismatch",
            "AudioDestinationRevisionMismatch": "audio_destination_revision_mismatch",
            "AudioDestinationAlreadySettled": "audio_destination_settled",
        }.get(error_type, "audio_destination_conflict")
        raise HTTPException(
            status_code=410 if error_type == "AudioDestinationAlreadySettled" else 409,
            detail={"error": error_name, "message": str(cause)},
        ) from exc
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        raise HTTPException(
            status_code=404,
            detail=f"Audio workflow {workflow_id!r} was not found.",
        ) from exc


async def _cancel_audio_generation(
    temporal: Client,
    workflow_id: str,
) -> AudioGenerationSnapshot:
    current = await _query_audio_snapshot(temporal, workflow_id)
    if current.state != "running":
        return current
    handle = temporal.get_workflow_handle(workflow_id)
    try:
        await handle.cancel()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return await _query_audio_snapshot_from_handle(handle, workflow_id)
    return await _query_audio_snapshot_from_handle(handle, workflow_id)
