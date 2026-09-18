"""Deletion uses live status, preserves files by default, and survives retries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from temporalio.service import RPCError, RPCStatusCode

from sdlc_builder.server import create_app
from sdlc_builder.store import Store, workspace

TASK_ID = "d" * 32


@pytest.fixture
async def context(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    app = create_app()
    store = app.state.store
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.txt").write_text("source stays intact")
    root = workspace(TASK_ID)
    root.mkdir(parents=True)
    (root / "work.txt").write_text("valuable unfinished change")
    # An old or altered saved path must never direct deletion into the source.
    store.put(
        "tasks",
        {
            "id": TASK_ID,
            "workflow_id": "sdlc-" + TASK_ID,
            "dispatch": "submitted",
            "workspace": str(source),
        },
    )
    snapshot = {"version": 8, "value": {"status": "review"}}
    store.archive(TASK_ID, snapshot)
    handle = SimpleNamespace(
        query=AsyncMock(return_value=snapshot), terminate=AsyncMock()
    )
    app.state.client = SimpleNamespace(get_workflow_handle=lambda _: handle)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"X-SDLC": "1"},
    ) as client:
        token = (tmp_path / "session-token").read_text()
        assert (
            await client.post("/api/unlock", json={"token": token})
        ).status_code == 200
        yield SimpleNamespace(
            client=client,
            store=store,
            handle=handle,
            root=root,
            source=source,
        )


async def delete(context, remove=False):
    return await context.client.request(
        "DELETE",
        "/api/tasks/" + TASK_ID,
        json={"remove_workspace": remove},
    )


@pytest.mark.parametrize("remove", [False, True])
async def test_delete_preserves_or_removes_only_the_selected_clone(context, remove):
    # Symlinked content must be unlinked, never followed into the source.
    (context.root / "link").symlink_to(context.source, target_is_directory=True)
    other = workspace("e" * 32)
    other.mkdir()
    response = await delete(context, remove)
    assert response.status_code == 200, response.text
    assert response.json()["workspace_removed"] is remove
    assert context.root.exists() is not remove
    assert other.exists()
    assert (context.source / "source.txt").read_text() == "source stays intact"
    context.handle.terminate.assert_awaited_once()
    # A late collector snapshot cannot recreate the deleted catalogue row.
    context.store.archive(TASK_ID, {"version": 20, "value": {}})
    assert Store().all("tasks") == []


@pytest.mark.parametrize("status", ["running", "paused", "needs_you", "queued"])
async def test_stale_finished_snapshot_does_not_allow_active_deletion(context, status):
    context.handle.query.return_value = {"version": 9, "value": {"status": status}}
    response = await delete(context, True)
    assert response.status_code == 409 and "Stop" in response.text
    assert context.root.exists() and context.store.all("tasks")
    context.handle.terminate.assert_not_awaited()


@pytest.mark.parametrize("stage", ["query", "terminate"])
async def test_unavailable_temporal_keeps_task_and_files(context, stage):
    getattr(context.handle, stage).side_effect = RPCError(
        "private upstream message",
        RPCStatusCode.UNAVAILABLE,
        b"",
    )
    response = await delete(context, True)
    assert response.status_code == 503
    assert "private upstream message" not in response.text
    assert context.root.exists() and context.store.all("tasks")


async def test_cleanup_failure_can_be_retried_after_workflow_closed(
    context, monkeypatch
):
    import sdlc_builder.server as server

    rmtree = server.shutil.rmtree

    def denied(_):
        raise PermissionError("private path")

    monkeypatch.setattr(server.shutil, "rmtree", denied)
    response = await delete(context, True)
    assert response.status_code == 409
    assert "could not be fully removed" in response.text
    assert context.store.all("tasks") and context.root.exists()
    for method in [context.handle.query, context.handle.terminate]:
        method.side_effect = RPCError("closed", RPCStatusCode.NOT_FOUND, b"")
    monkeypatch.setattr(server.shutil, "rmtree", rmtree)
    response = await delete(context, True)
    assert response.status_code == 200
    assert not context.root.exists() and not context.store.all("tasks")


async def test_refuse_symlink_workspace_root(context):
    (context.root / "work.txt").unlink()
    context.root.rmdir()
    context.root.symlink_to(context.source, target_is_directory=True)
    response = await delete(context, True)
    assert response.status_code == 400
    assert context.store.all("tasks") and (context.source / "source.txt").exists()
    context.handle.terminate.assert_not_awaited()


async def test_interrupted_preparation_can_be_removed_without_temporal(context):
    task = context.store.get("tasks", TASK_ID)
    task["dispatch"] = "preparing"
    context.store.put("tasks", task)
    response = await delete(context, True)
    assert response.status_code == 200
    context.handle.query.assert_not_awaited()
    context.handle.terminate.assert_not_awaited()


async def test_deletion_serializes_with_control_and_requires_auth(context):
    reached = asyncio.Event()
    release = asyncio.Event()

    async def delayed_query(*_args, **_kwargs):
        reached.set()
        await release.wait()
        return {"version": 9, "value": {"status": "cancelled"}}

    context.handle.query.side_effect = delayed_query
    context.handle.execute_update = AsyncMock()
    deletion = asyncio.create_task(delete(context))
    await asyncio.wait_for(reached.wait(), 2)
    control = asyncio.create_task(
        context.client.post(
            f"/api/tasks/{TASK_ID}/control",
            json={"command": "resume"},
        )
    )
    await asyncio.sleep(0)
    release.set()
    assert (await deletion).status_code == 200
    assert (await control).status_code == 400
    context.handle.execute_update.assert_not_awaited()
    context.client.cookies.clear()
    assert (await delete(context)).status_code == 401
