"""Workflow-owned project merge authorization, activity safety, and API routing."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from temporal_agent_harness.harness.state import StateRef

from sdlc_builder import coding_workflow as coding_workflow_module
from sdlc_builder.coding_models import (
    CodingState,
    ProjectMergeReceipt,
    ProjectMergeResult,
)
from sdlc_builder.coding_workflow import CodingWorkflow
from sdlc_builder.coding_workspace import project_merge_operation, revision
from sdlc_builder.models import Profile, TaskInput
from sdlc_builder.server import create_app
from sdlc_builder.store import workspace
from sdlc_builder.workspaces import git, prepare_workspace

TASK_ID = "f" * 32
PROJECT_ID = "project"


@pytest.fixture
async def context(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path / "data"))
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.txt").write_text(
        "task line\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nsource line\n"
    )
    (source / "remove.txt").write_text("remove me\n")
    git(source, "init", "-b", "main")
    git(source, "add", ".")
    git(
        source,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "Base",
    )
    base = git(source, "rev-parse", "HEAD").strip()

    app = create_app()
    prepared = prepare_workspace(str(source), TASK_ID)
    task_root = workspace(TASK_ID)
    app.state.store.put(
        "projects",
        {
            "id": PROJECT_ID,
            "name": "source",
            "path": str(source),
            "github_url": "",
        },
    )
    app.state.store.put(
        "tasks",
        {
            "id": TASK_ID,
            "workflow_id": "sdlc-" + TASK_ID,
            "engine": "v2",
            "project_id": PROJECT_ID,
            "project_name": "source",
            "title": "Change the project",
            "mode": "change",
            "profile_id": "demo",
            "dispatch": "submitted",
            **prepared,
        },
    )
    envelope = {
        "version": 7,
        "value": {
            "status": "accepted",
            "mode": "change",
            "revision": revision(task_root),
        },
        "can_message": True,
        "next_turn": 2,
    }
    handle = SimpleNamespace(
        query=AsyncMock(return_value=envelope),
        execute_update=AsyncMock(),
    )
    app.state.client = SimpleNamespace(get_workflow_handle=lambda _: handle)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"X-SDLC": "1"},
    ) as client:
        token = (tmp_path / "data" / "session-token").read_text()
        assert (
            await client.post("/api/unlock", json={"token": token})
        ).status_code == 200
        yield SimpleNamespace(
            app=app,
            client=client,
            source=source,
            task_root=task_root,
            envelope=envelope,
            handle=handle,
            base=base,
        )


async def test_merge_activity_preserves_local_edits_and_recovers_idempotently(context):
    task_lines = (context.task_root / "app.txt").read_text().splitlines()
    task_lines[0] = "accepted task line"
    (context.task_root / "app.txt").write_text("\n".join(task_lines) + "\n")
    (context.task_root / "remove.txt").unlink()
    (context.task_root / "new.txt").write_text("new task file\n")
    source_lines = (context.source / "app.txt").read_text().splitlines()
    source_lines[-1] = "uncommitted source line"
    (context.source / "app.txt").write_text("\n".join(source_lines) + "\n")
    accepted_revision = revision(context.task_root)

    result = await project_merge_operation(TASK_ID, "merge-1", accepted_revision)
    assert result.ok and result.receipt
    receipt = result.receipt
    assert not receipt.already_applied
    assert receipt.branch == "main"
    assert receipt.files == ["app.txt", "new.txt", "remove.txt"]
    merged = (context.source / "app.txt").read_text()
    assert merged.startswith("accepted task line\n")
    assert merged.endswith("uncommitted source line\n")
    assert (context.source / "new.txt").read_text() == "new task file\n"
    assert not (context.source / "remove.txt").exists()
    assert git(context.source, "rev-parse", "HEAD").strip() == context.base

    # A delivered activity result is returned byte-for-byte from its journal.
    assert (
        await project_merge_operation(TASK_ID, "merge-1", accepted_revision) == result
    )
    # If the worker applied the patch and died before recording completion, a
    # Temporal retry recognizes the already-present patch and records success.
    record = (
        context.task_root.parents[1]
        / "project-merge-operations"
        / TASK_ID
        / "merge-1.json"
    )
    interrupted = json.loads(record.read_text())
    del interrupted["result"]
    record.write_text(json.dumps(interrupted))
    recovered = await project_merge_operation(TASK_ID, "merge-1", accepted_revision)
    assert recovered.ok and recovered.receipt.already_applied
    assert (context.source / "app.txt").read_text() == merged


async def test_merge_activity_rejects_conflict_without_writing(context):
    (context.task_root / "app.txt").write_text(
        (context.task_root / "app.txt")
        .read_text()
        .replace("task line", "accepted task line")
    )
    (context.source / "app.txt").write_text(
        (context.source / "app.txt")
        .read_text()
        .replace("task line", "local project line")
    )
    before = (context.source / "app.txt").read_text()

    result = await project_merge_operation(
        TASK_ID,
        "merge-conflict",
        revision(context.task_root),
    )
    assert not result.ok
    assert "overlapping changes" in result.error
    assert (context.source / "app.txt").read_text() == before


async def test_merge_activity_requires_accepted_revision_and_supported_files(context):
    accepted_revision = revision(context.task_root)
    (context.task_root / "app.txt").write_text("changed after acceptance\n")
    stale = await project_merge_operation(TASK_ID, "merge-stale", accepted_revision)
    assert not stale.ok and "changed after acceptance" in stale.error
    assert (context.source / "app.txt").read_text().startswith("task line\n")

    (context.task_root / "binary.bin").write_bytes(b"\0private")
    unsupported = await project_merge_operation(
        TASK_ID,
        "merge-unsupported",
        revision(context.task_root),
    )
    assert not unsupported.ok
    assert "cannot be merged automatically" in unsupported.error
    assert not (context.source / "binary.bin").exists()


def workflow_controller(revision_value: str) -> CodingWorkflow:
    obj = CodingWorkflow.__new__(CodingWorkflow)
    obj.current_task = TaskInput(
        task_id=TASK_ID,
        prompt="Test",
        mode="change",
        profile=Profile(id="test", label="test", provider="demo", max_steps=3),
    )
    obj.state = StateRef(
        "sdlc",
        CodingState(
            task_id=TASK_ID,
            mode="change",
            status="accepted",
            phase="complete",
            focus="Accepted locally",
            next_action="Merge into project",
            revision=revision_value,
        ),
    )
    obj.lock = asyncio.Lock()
    return obj


async def test_workflow_authorizes_activity_and_records_receipt(context, monkeypatch):
    accepted_revision = revision(context.task_root)
    receipt = ProjectMergeReceipt(
        operation_id="workflow-operation",
        base_revision=context.base,
        revision=accepted_revision,
        files=["app.txt"],
        applied_files=["app.txt"],
        project_path=str(context.source),
        branch="main",
        merged_at=1.0,
    )
    execute_activity = AsyncMock(return_value=ProjectMergeResult(receipt=receipt))
    monkeypatch.setattr(
        coding_workflow_module.workflow,
        "uuid4",
        lambda: SimpleNamespace(hex="workflow-operation"),
    )
    monkeypatch.setattr(
        coding_workflow_module.workflow, "execute_activity", execute_activity
    )
    controller = workflow_controller(accepted_revision)

    result = await controller.merge_project()
    assert result == receipt.model_dump(mode="json")
    assert controller.state.current.project_merge == receipt
    assert controller.state.current.focus == "Merged into project"
    args = execute_activity.await_args.kwargs["args"]
    assert args == [TASK_ID, "workflow-operation", accepted_revision]

    # Repeated user requests are answered from workflow state without another
    # filesystem activity, even if the HTTP response was previously lost.
    assert await controller.merge_project() == result
    execute_activity.assert_awaited_once()


async def test_workflow_rejects_merge_before_acceptance(context, monkeypatch):
    execute_activity = AsyncMock()
    monkeypatch.setattr(
        coding_workflow_module.workflow, "execute_activity", execute_activity
    )
    controller = workflow_controller(revision(context.task_root))
    with controller.state.mutate() as state:
        state.status = "review"

    result = await controller.merge_project()
    assert not result["ok"] and "Accept" in result["error"]
    execute_activity.assert_not_awaited()


async def test_api_delegates_merge_to_workflow_without_touching_project(context):
    receipt = ProjectMergeReceipt(
        operation_id="workflow-operation",
        base_revision=context.base,
        revision=revision(context.task_root),
        files=["app.txt"],
        applied_files=["app.txt"],
        project_path=str(context.source),
        branch="main",
        merged_at=1.0,
    ).model_dump(mode="json")
    context.handle.execute_update.return_value = receipt
    before = (context.source / "app.txt").read_text()

    response = await context.client.post(f"/api/tasks/{TASK_ID}/merge")
    assert response.status_code == 200, response.text
    assert response.json() == receipt
    assert (context.source / "app.txt").read_text() == before
    assert context.handle.execute_update.await_args.args == ("merge_project",)
    assert "project_merge" not in context.app.state.store.get("tasks", TASK_ID)
