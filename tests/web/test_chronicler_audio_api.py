from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from examples.app import create_app as create_aggregate_app
from examples.chronicler.audio_models import (
    AudioApprovalPackage,
    AudioDestinationApproval,
    AudioGenerationResult,
    AudioGenerationSnapshot,
    AudioGenerationStatus,
    DestinationRevision,
)
from examples.chronicler.audio_workflow import (
    APPROVE_AUDIO_DESTINATION_UPDATE,
    AUDIO_STATUS_QUERY,
)
from examples.chronicler.app import create_app as create_chronicler_app
from temporal_agent_harness.web import AgentRegistry

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _inject_chronicler_ui_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep API tests independent from the generated example UI bundle."""
    from examples.chronicler import app as chronicler_app

    static_dir = tmp_path / "ui_dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<title>test Chronicler UI</title>")
    monkeypatch.setattr(chronicler_app, "_UI_DIST", static_dir)


class _AudioHandle:
    def __init__(self, snapshot: AudioGenerationSnapshot) -> None:
        self.snapshot = snapshot
        self.query_error: BaseException | None = None
        self.update_error: BaseException | None = None
        self.query_calls: list[tuple[Any, dict[str, Any]]] = []
        self.update_calls: list[tuple[Any, Any, dict[str, Any]]] = []
        self.cancel_calls = 0
        self.cancel_error: BaseException | None = None
        self.snapshot_after_cancel: AudioGenerationSnapshot | None = None

    async def query(self, query: Any, **kwargs: Any) -> AudioGenerationSnapshot:
        self.query_calls.append((query, kwargs))
        if self.query_error is not None:
            raise self.query_error
        return self.snapshot

    async def execute_update(
        self, update: Any, arg: Any, **kwargs: Any
    ) -> AudioGenerationSnapshot:
        self.update_calls.append((update, arg, kwargs))
        if self.update_error is not None:
            raise self.update_error
        return self.snapshot

    async def cancel(self) -> None:
        self.cancel_calls += 1
        if self.snapshot_after_cancel is not None:
            self.snapshot = self.snapshot_after_cancel
        if self.cancel_error is not None:
            raise self.cancel_error


class _TemporalClient:
    def __init__(self, handle: _AudioHandle) -> None:
        self.handle = handle
        self.workflow_ids: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> _AudioHandle:
        self.workflow_ids.append(workflow_id)
        return self.handle


def _client(snapshot: AudioGenerationSnapshot) -> tuple[TestClient, _TemporalClient, _AudioHandle]:
    handle = _AudioHandle(snapshot)
    temporal = _TemporalClient(handle)
    app = create_chronicler_app(registry=AgentRegistry())
    app.state.temporal = temporal
    return TestClient(app), temporal, handle


def _approved_package(generation_id: str) -> AudioApprovalPackage:
    content = "The party crossed the bridge."
    return AudioApprovalPackage(
        package_revision=1,
        generation_id=generation_id,
        source_kind="existing",
        source_identity="sessions/session-7.md",
        source_content=content,
        source_hash=hashlib.sha256(content.encode()).hexdigest(),
        recap_script="Previously, the party crossed the bridge.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )


def test_aggregate_app_does_not_expose_chronicler_audio_routes() -> None:
    app = create_aggregate_app(
        ROOT / "examples" / "openai_hello" / "agents.toml",
        ROOT / "examples" / "chronicler" / "agents.toml",
    )
    client = TestClient(app)

    response = client.get(
        "/api/chronicler/audio/status",
        params={"workflow_id": "not-a-chronicler-audio-workflow"},
    )

    assert response.status_code == 404


def _running_snapshot() -> AudioGenerationSnapshot:
    package = _approved_package("generation-chat-7")
    return AudioGenerationSnapshot(
        child_workflow_id="chronicler-audio--campaigns/acme/chat-7",
        state="running",
        status=AudioGenerationStatus(
            generation_id="generation-chat-7",
            child_workflow_id="chronicler-audio--campaigns/acme/chat-7",
            phase="generating_audio",
        ),
        approved_package=package,
    )


def _canceled_snapshot() -> AudioGenerationSnapshot:
    status = AudioGenerationStatus(
        generation_id="generation-chat-7",
        child_workflow_id="chronicler-audio--campaigns/acme/chat-7",
        phase="canceled",
    )
    package = _approved_package(status.generation_id)
    return AudioGenerationSnapshot(
        child_workflow_id=status.child_workflow_id,
        state="canceled",
        status=status,
        approved_package=package,
        result=AudioGenerationResult(
            generation_id=status.generation_id,
            outcome="canceled",
            status=status,
            approved_package=package,
        ),
    )


def _completed_snapshot() -> AudioGenerationSnapshot:
    status = AudioGenerationStatus(
        generation_id="generation-chat-7",
        child_workflow_id="chronicler-audio--campaigns/acme/chat-7",
        phase="complete",
    )
    package = _approved_package(status.generation_id)
    return AudioGenerationSnapshot(
        child_workflow_id=status.child_workflow_id,
        state="completed",
        status=status,
        approved_package=package,
        result=AudioGenerationResult(
            generation_id=status.generation_id,
            outcome="completed",
            status=status,
            approved_package=package,
        ),
    )


def test_audio_status_returns_typed_snapshot_for_opaque_workflow_id() -> None:
    snapshot = _running_snapshot()
    client, temporal, handle = _client(snapshot)

    response = client.get(
        "/api/chronicler/audio/status",
        params={"workflow_id": snapshot.child_workflow_id},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == snapshot.model_dump(mode="json")
    assert temporal.workflow_ids == [snapshot.child_workflow_id]
    assert handle.query_calls == [
        (AUDIO_STATUS_QUERY, {"result_type": AudioGenerationSnapshot})
    ]


def test_audio_status_rejects_a_mid_id_audio_prefix_before_temporal_access() -> None:
    client, temporal, _handle = _client(_running_snapshot())

    response = client.get(
        "/api/chronicler/audio/status",
        params={"workflow_id": "campaigns/acme/chronicler-audio--chat-7"},
    )

    assert response.status_code == 422
    assert temporal.workflow_ids == []


def test_audio_status_reports_a_missing_child() -> None:
    client, _temporal, handle = _client(_running_snapshot())
    handle.query_error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    response = client.get(
        "/api/chronicler/audio/status",
        params={"workflow_id": "chronicler-audio--campaigns/acme/missing-child"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == (
        "Audio workflow 'chronicler-audio--campaigns/acme/missing-child' was not found."
    )


def test_audio_destination_approves_a_typed_revision_for_an_opaque_workflow_id() -> None:
    snapshot = _running_snapshot()
    client, temporal, handle = _client(snapshot)
    payload = {
        "workflow_id": snapshot.child_workflow_id,
        "generation_id": "generation-chat-7",
        "content_digest": "c" * 64,
        "destination_revision": 2,
        "wav_path": "audio/session-7-recap-v2.wav",
        "synthetic_markdown_path": None,
        "bridge_id": "browser-local",
        "root_id": "campaign-root",
        "folder_binding_id": "binding-7",
    }

    response = client.post("/api/chronicler/audio/destination", json=payload)

    assert response.status_code == 200
    assert response.json() == snapshot.model_dump(mode="json")
    assert temporal.workflow_ids == [snapshot.child_workflow_id]
    update, approval, kwargs = handle.update_calls[0]
    assert update == APPROVE_AUDIO_DESTINATION_UPDATE
    assert approval == AudioDestinationApproval(
        revision=DestinationRevision(
            generation_id=payload["generation_id"],
            content_digest=payload["content_digest"],
            destination_revision=payload["destination_revision"],
            wav_path=payload["wav_path"],
            synthetic_markdown_path=None,
        ),
        bridge_id=payload["bridge_id"],
        root_id=payload["root_id"],
        folder_binding_id=payload["folder_binding_id"],
    )
    assert kwargs["result_type"] is AudioGenerationSnapshot


def test_audio_destination_rejects_a_lookalike_prefix_before_temporal_access() -> None:
    client, temporal, _handle = _client(_running_snapshot())

    response = client.post(
        "/api/chronicler/audio/destination",
        json={
            "workflow_id": "chronicler-audio-chat-7",
            "generation_id": "generation-chat-7",
            "content_digest": "c" * 64,
            "destination_revision": 2,
            "wav_path": "audio/session-7-recap-v2.wav",
            "synthetic_markdown_path": None,
            "bridge_id": "browser-local",
            "root_id": "campaign-root",
            "folder_binding_id": "binding-7",
        },
    )

    assert response.status_code == 422
    assert temporal.workflow_ids == []


def test_audio_destination_rejects_wav_path_traversal_as_request_validation() -> None:
    client, temporal, _handle = _client(_running_snapshot())

    response = client.post(
        "/api/chronicler/audio/destination",
        json={
            "workflow_id": "chronicler-audio--campaigns/acme/chat-7",
            "generation_id": "generation-chat-7",
            "content_digest": "c" * 64,
            "destination_revision": 2,
            "wav_path": "../session-7-recap-v2.wav",
            "synthetic_markdown_path": None,
            "bridge_id": "browser-local",
            "root_id": "campaign-root",
            "folder_binding_id": "binding-7",
        },
    )

    assert response.status_code == 422
    assert temporal.workflow_ids == []


def test_audio_destination_rejects_a_nonpositive_revision_as_request_validation() -> None:
    client, temporal, _handle = _client(_running_snapshot())

    response = client.post(
        "/api/chronicler/audio/destination",
        json={
            "workflow_id": "chronicler-audio--campaigns/acme/chat-7",
            "generation_id": "generation-chat-7",
            "content_digest": "c" * 64,
            "destination_revision": 0,
            "wav_path": "audio/session-7-recap-v2.wav",
            "synthetic_markdown_path": None,
            "bridge_id": "browser-local",
            "root_id": "campaign-root",
            "folder_binding_id": "binding-7",
        },
    )

    assert response.status_code == 422
    assert temporal.workflow_ids == []


def test_audio_destination_rejects_a_non_wav_extension_as_request_validation() -> None:
    client, temporal, _handle = _client(_running_snapshot())

    response = client.post(
        "/api/chronicler/audio/destination",
        json={
            "workflow_id": "chronicler-audio--campaigns/acme/chat-7",
            "generation_id": "generation-chat-7",
            "content_digest": "c" * 64,
            "destination_revision": 2,
            "wav_path": "audio/session-7-recap-v2.mp3",
            "synthetic_markdown_path": None,
            "bridge_id": "browser-local",
            "root_id": "campaign-root",
            "folder_binding_id": "binding-7",
        },
    )

    assert response.status_code == 422
    assert temporal.workflow_ids == []


def test_audio_destination_rejects_non_sibling_markdown_as_request_validation() -> None:
    client, temporal, _handle = _client(_running_snapshot())

    response = client.post(
        "/api/chronicler/audio/destination",
        json={
            "workflow_id": "chronicler-audio--campaigns/acme/chat-7",
            "generation_id": "generation-chat-7",
            "content_digest": "c" * 64,
            "destination_revision": 2,
            "wav_path": "audio/session-7-recap-v2.wav",
            "synthetic_markdown_path": "notes/session-7-recap-v2.md",
            "bridge_id": "browser-local",
            "root_id": "campaign-root",
            "folder_binding_id": "binding-7",
        },
    )

    assert response.status_code == 422
    assert temporal.workflow_ids == []


def test_audio_destination_maps_binding_mismatch_to_conflict() -> None:
    snapshot = _running_snapshot()
    client, _temporal, handle = _client(snapshot)
    handle.update_error = WorkflowUpdateFailedError(
        ApplicationError(
            "destination approval binding does not match the audio package",
            type="AudioBindingMismatch",
        )
    )

    response = client.post(
        "/api/chronicler/audio/destination",
        json={
            "workflow_id": snapshot.child_workflow_id,
            "generation_id": "generation-chat-7",
            "content_digest": "c" * 64,
            "destination_revision": 2,
            "wav_path": "audio/session-7-recap-v2.wav",
            "synthetic_markdown_path": None,
            "bridge_id": "browser-other",
            "root_id": "campaign-root",
            "folder_binding_id": "binding-other",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "audio_binding_mismatch"


def test_audio_destination_maps_phase_mismatch_to_stable_conflict() -> None:
    snapshot = _running_snapshot()
    client, _temporal, handle = _client(snapshot)
    handle.update_error = WorkflowUpdateFailedError(
        ApplicationError(
            "audio generation is not awaiting destination approval",
            type="AudioDestinationPhaseMismatch",
        )
    )

    response = client.post(
        "/api/chronicler/audio/destination",
        json={
            "workflow_id": snapshot.child_workflow_id,
            "generation_id": "generation-chat-7",
            "content_digest": "c" * 64,
            "destination_revision": 2,
            "wav_path": "audio/session-7-recap-v2.wav",
            "synthetic_markdown_path": None,
            "bridge_id": "browser-local",
            "root_id": "campaign-root",
            "folder_binding_id": "binding-7",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "audio_destination_phase_mismatch"


def test_audio_destination_reports_an_already_settled_terminal_race() -> None:
    snapshot = _running_snapshot()
    client, _temporal, handle = _client(snapshot)
    handle.update_error = WorkflowUpdateFailedError(
        ApplicationError(
            "audio generation is already complete",
            type="AudioDestinationAlreadySettled",
        )
    )

    response = client.post(
        "/api/chronicler/audio/destination",
        json={
            "workflow_id": snapshot.child_workflow_id,
            "generation_id": "generation-chat-7",
            "content_digest": "c" * 64,
            "destination_revision": 2,
            "wav_path": "audio/session-7-recap-v2.wav",
            "synthetic_markdown_path": None,
            "bridge_id": "browser-local",
            "root_id": "campaign-root",
            "folder_binding_id": "binding-7",
        },
    )

    assert response.status_code == 410
    assert response.json()["detail"]["error"] == "audio_destination_settled"


def test_audio_cancel_returns_the_authoritative_canceled_snapshot() -> None:
    client, temporal, handle = _client(_running_snapshot())
    handle.snapshot_after_cancel = _canceled_snapshot()

    response = client.post(
        "/api/chronicler/audio/cancel",
        json={"workflow_id": "chronicler-audio--campaigns/acme/chat-7"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == _canceled_snapshot().model_dump(mode="json")
    assert temporal.workflow_ids == [
        "chronicler-audio--campaigns/acme/chat-7",
        "chronicler-audio--campaigns/acme/chat-7",
    ]
    assert handle.cancel_calls == 1


def test_audio_cancel_rejects_a_mid_id_audio_prefix_before_temporal_access() -> None:
    client, temporal, handle = _client(_running_snapshot())

    response = client.post(
        "/api/chronicler/audio/cancel",
        json={"workflow_id": "prefix-chronicler-audio--chat-7"},
    )

    assert response.status_code == 422
    assert temporal.workflow_ids == []
    assert handle.cancel_calls == 0


def test_audio_cancel_preserves_an_already_completed_terminal_result() -> None:
    completed = _completed_snapshot()
    client, _temporal, handle = _client(completed)

    response = client.post(
        "/api/chronicler/audio/cancel",
        json={"workflow_id": completed.child_workflow_id},
    )

    assert response.status_code == 200
    assert response.json() == completed.model_dump(mode="json")
    assert handle.cancel_calls == 0


def test_audio_cancel_reports_a_missing_child_without_canceling() -> None:
    client, _temporal, handle = _client(_running_snapshot())
    handle.query_error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    response = client.post(
        "/api/chronicler/audio/cancel",
        json={"workflow_id": "chronicler-audio--campaigns/acme/missing-child"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == (
        "Audio workflow 'chronicler-audio--campaigns/acme/missing-child' was not found."
    )
    assert handle.cancel_calls == 0


def test_audio_cancel_does_not_misclassify_unexpected_temporal_errors() -> None:
    client, _temporal, handle = _client(_running_snapshot())
    handle.cancel_error = RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")

    with pytest.raises(RPCError) as failure:
        client.post(
            "/api/chronicler/audio/cancel",
            json={"workflow_id": "chronicler-audio--campaigns/acme/chat-7"},
        )

    assert failure.value.status == RPCStatusCode.UNAVAILABLE


def test_audio_destination_reports_a_missing_child() -> None:
    snapshot = _running_snapshot()
    client, _temporal, handle = _client(snapshot)
    handle.update_error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    response = client.post(
        "/api/chronicler/audio/destination",
        json={
            "workflow_id": "chronicler-audio--campaigns/acme/missing-child",
            "generation_id": "generation-chat-7",
            "content_digest": "c" * 64,
            "destination_revision": 2,
            "wav_path": "audio/session-7-recap-v2.wav",
            "synthetic_markdown_path": None,
            "bridge_id": "browser-local",
            "root_id": "campaign-root",
            "folder_binding_id": "binding-7",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == (
        "Audio workflow 'chronicler-audio--campaigns/acme/missing-child' was not found."
    )


def test_audio_cancel_returns_terminal_state_when_the_child_closes_during_cancel() -> None:
    completed = _completed_snapshot()
    client, _temporal, handle = _client(_running_snapshot())
    handle.snapshot_after_cancel = completed
    handle.cancel_error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    response = client.post(
        "/api/chronicler/audio/cancel",
        json={"workflow_id": completed.child_workflow_id},
    )

    assert response.status_code == 200
    assert response.json() == completed.model_dump(mode="json")
    assert handle.cancel_calls == 1
