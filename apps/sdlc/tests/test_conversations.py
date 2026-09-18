"""Conversation retention and delivery across retries and competing clients."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError

from sdlc_builder.conversations import CONTEXT_CHAR_LIMIT, recent_context
from sdlc_builder.server import create_app

TASK_ID = "c" * 32
MESSAGE = {
    "id": "b" * 32,
    "expected_turn": 2,
    "prompt": "Explain the last change",
    "mode": "ask",
    "profile_id": "demo",
    "max_steps": 200,
}


def test_context_keeps_original_and_recent_whole_records_without_mutation():
    history = [
        {"role": "you", "text": "Original feature requirements"},
        {"role": "tool", "text": "x" * CONTEXT_CHAR_LIMIT},
        {"role": "you", "text": "Refine that feature"},
        {"role": "tool", "text": '{"content": "latest file"}'},
    ]
    original = deepcopy(history)
    result, shortened = recent_context(history)
    assert shortened and result[0] == history[0] and result[-2:] == history[-2:]
    assert sum(len(item["text"]) for item in result) <= CONTEXT_CHAR_LIMIT
    result[-1]["text"] = "changed locally"
    assert history == original
    assert recent_context([]) == ([], False)
    assert recent_context(history[-2:]) == (history[-2:], False)


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    app = create_app()
    store = app.state.store
    store.put("projects", {"id": "demo-project", "demo": True})
    store.put(
        "tasks",
        {
            "id": TASK_ID,
            "workflow_id": "sdlc-" + TASK_ID,
            "project_id": "demo-project",
            "dispatch": "submitted",
            "profile_id": "demo",
        },
    )
    snapshot = {
        "version": 10,
        "value": {"status": "review"},
        "can_message": True,
        "next_turn": 2,
    }
    handle = SimpleNamespace(
        query=AsyncMock(return_value=snapshot),
        execute_update=AsyncMock(return_value=SimpleNamespace(turn_number=2)),
    )
    app.state.client = SimpleNamespace(get_workflow_handle=lambda _: handle)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"X-SDLC": "1"},
    ) as client:
        await client.post(
            "/api/unlock", json={"token": (tmp_path / "session-token").read_text()}
        )
        yield SimpleNamespace(client=client, handle=handle, store=store, app=app)


async def send(ctx, **changes):
    return await ctx.client.post(
        "/api/tasks/" + TASK_ID + "/messages", json={**MESSAGE, **changes}
    )


async def test_receipt_prevents_duplicate_execution_and_id_reuse(ctx):
    result = await send(ctx)
    assert result.status_code == 200 and result.json()["status"] == "accepted"
    assert (await send(ctx)).json()["turn_number"] == 2
    ctx.handle.execute_update.assert_awaited_once()
    args, kwargs = ctx.handle.execute_update.call_args
    assert args[1].type == "follow_up"
    assert args[1].payload["profile"]["max_steps"] == 200
    assert kwargs["id"] == "sdlc-message-" + MESSAGE["id"]
    assert (await send(ctx, prompt="Different request")).status_code == 409
    assert "pending_message" not in ctx.store.get("tasks", TASK_ID)


async def test_lost_receipt_retries_same_update_from_persisted_outbox(ctx):
    ids = []

    async def uncertain(*args, **kwargs):
        # Simulate Temporal accepting the update but the first response being lost.
        assert (
            ctx.store.get("tasks", TASK_ID)["pending_message"]["payload"]["prompt"]
            == MESSAGE["prompt"]
        )
        ids.append(kwargs["id"])
        if len(ids) == 1:
            raise ConnectionError("secret upstream details")
        return SimpleNamespace(turn_number=2)

    ctx.handle.execute_update.side_effect = uncertain
    response = await send(ctx)
    assert response.json()["status"] == "pending"
    assert "secret upstream" not in response.text
    assert (await send(ctx, id="d" * 32)).status_code == 409
    assert (
        await ctx.client.post(
            "/api/tasks/" + TASK_ID + "/control", json={"command": "accept"}
        )
    ).status_code == 409
    assert (await ctx.client.delete("/api/tasks/" + TASK_ID)).status_code == 409
    # A fresh app instance loads the same persisted outbox after a restart.
    restarted = create_app()
    restarted.state.client = ctx.app.state.client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted),
        base_url="http://localhost",
        headers={"X-SDLC": "1"},
        cookies=ctx.client.cookies,
    ) as client:
        result = await client.post("/api/tasks/" + TASK_ID + "/messages", json=MESSAGE)
        assert result.json()["status"] == "accepted"
    assert ids == ["sdlc-message-" + MESSAGE["id"]] * 2


@pytest.mark.parametrize("live,next_turn", [(False, 2), (True, 3)])
async def test_busy_or_stale_client_cannot_send(ctx, live, next_turn):
    ctx.handle.query.return_value.update(can_message=live, next_turn=next_turn)
    assert (await send(ctx)).status_code == 409
    ctx.handle.execute_update.assert_not_awaited()


async def test_unavailable_temporal_and_invalid_messages_do_not_create_outbox(ctx):
    for changes in [
        {"prompt": "  "},
        {"max_steps": 201},
        {"mode": "anything"},
        {"expected_turn": 1},
    ]:
        assert (await send(ctx, **changes)).status_code == 422
    ctx.handle.query.side_effect = ConnectionError("private host")
    assert (await send(ctx)).status_code == 503
    assert "pending_message" not in ctx.store.get("tasks", TASK_ID)
    ctx.handle.execute_update.assert_not_awaited()


async def test_rejected_update_retains_message_for_recovery(ctx):
    ctx.handle.execute_update.side_effect = WorkflowUpdateFailedError(
        ApplicationError("private upstream error")
    )
    result = await send(ctx)
    assert (
        result.json()["status"] == "rejected" and "private upstream" not in result.text
    )
    task = ctx.store.get("tasks", TASK_ID)
    assert task["message_error"]["prompt"] == MESSAGE["prompt"]
    assert "pending_message" not in task
    assert (await send(ctx)).json()["status"] == "rejected"
    ctx.handle.execute_update.assert_awaited_once()


async def test_concurrent_duplicate_requests_execute_once(ctx):
    first, second = await asyncio.gather(send(ctx), send(ctx))
    assert first.json()["status"] == second.json()["status"] == "accepted"
    ctx.handle.execute_update.assert_awaited_once()


async def test_archives_conversation_changes_without_regressing_other_state(ctx):
    state = {
        "version": 10,
        "value": {"status": "review"},
        "conversation": {"version": 5, "value": {"turns": [{"status": "running"}]}},
    }
    ctx.store.archive(TASK_ID, state)
    newer = deepcopy(state)
    newer["conversation"] = {"version": 6, "value": {"turns": [{"status": "review"}]}}
    ctx.store.archive(TASK_ID, newer)
    ctx.store.archive(TASK_ID, state)
    assert ctx.store.get("tasks", TASK_ID)["snapshot"] == newer
