"""Human review stays bound to the inspected source across edits and restarts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from sdlc_builder import reviews
from sdlc_builder.server import create_app
from sdlc_builder.store import Store, workspace
from sdlc_builder.workspaces import demo_project, git, prepare_workspace

TASK = "a" * 32
PATH = f"/api/tasks/{TASK}/review"


@pytest.fixture
async def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    app = create_app()
    repo = workspace(TASK)
    prepared = prepare_workspace(str(demo_project()), TASK)
    app.state.store.put("projects", {"id": "demo-project", "demo": True})
    app.state.store.put(
        "tasks",
        {
            "id": TASK,
            "workflow_id": "sdlc-" + TASK,
            "profile_id": "demo",
            "project_id": "demo-project",
            "dispatch": "submitted",
            **prepared,
        },
    )
    handle = SimpleNamespace(
        query=AsyncMock(
            return_value={
                "version": 1,
                "value": {"status": "review"},
                "can_message": True,
                "next_turn": 2,
            }
        ),
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
        yield SimpleNamespace(
            repo=repo,
            app=app,
            client=client,
            store=app.state.store,
            handle=handle,
            root=tmp_path,
        )


async def detail(ctx, path="greeting.py", comparison="all"):
    response = await ctx.client.get(
        PATH + "/file", params={"path": path, "comparison": comparison}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def summary(ctx):
    response = await ctx.client.get(PATH)
    assert response.status_code == 200, response.text
    return response.json()


async def comment(ctx, value, **changes):
    body = {
        "id": "b" * 32,
        "path": value["path"],
        "token": value["token"],
        "comparison": value["comparison"],
        "side": "new",
        "line": 1,
        "text": "Please preserve Unicode names.",
        **changes,
    }
    response = await ctx.client.post(PATH + "/comments", json=body)
    return response, body


async def test_checkpoints_compare_exact_contents_and_survive_restart(ctx):
    (ctx.repo / "greeting.py").write_text("first\n")
    first = await detail(ctx)
    marked = await ctx.client.put(
        PATH + "/checkpoint",
        json={"path": first["path"], "token": first["source_token"]},
    )
    assert marked.status_code == 200
    assert (await summary(ctx))["files"][0]["reviewed"]
    assert (await detail(ctx, comparison="reviewed"))["lines"] == []
    (ctx.repo / "greeting.py").write_text("second\n")
    rows = (await summary(ctx))["files"]
    assert rows[0]["since_review"] and not rows[0]["reviewed"]
    assert rows[0]["review_additions"] == rows[0]["review_deletions"] == 1
    delta = await detail(ctx, comparison="reviewed")
    assert [(r["kind"], r["text"]) for r in delta["lines"][1:]] == [
        ("deletion", "first"),
        ("addition", "second"),
    ]
    stale = await ctx.client.put(
        PATH + "/checkpoint",
        json={"path": first["path"], "token": first["source_token"]},
    )
    assert stale.status_code == 409
    # A new Store instance reads the durable baseline, not the original base.
    saved = Store(ctx.root).review(TASK)
    assert saved["checkpoints"]["greeting.py"]["file"]["content"] == "first\n"
    # Returning to exactly the reviewed bytes restores the checkpoint indicator.
    (ctx.repo / "greeting.py").write_text("first\n")
    assert (await summary(ctx))["files"][0]["reviewed"]


async def test_comments_idempotent_and_anchored_across_edits(ctx):
    (ctx.repo / "greeting.py").write_text("reviewed line\n")
    value = await detail(ctx)
    (response, body), (duplicate, _) = await asyncio.gather(
        comment(ctx, value), comment(ctx, value)
    )
    assert response.status_code == duplicate.status_code == 200
    assert len((await summary(ctx))["comments"]) == 1
    assert (await summary(ctx))["version"] == 1
    reused, _ = await comment(ctx, value, text="Different content")
    assert reused.status_code == 409
    (ctx.repo / "greeting.py").write_text("inserted\nreviewed line\n")
    saved = (await summary(ctx))["comments"][0]
    assert (
        saved["line"] == 1 and saved["excerpt"] == "reviewed line" and saved["outdated"]
    )
    # A retry still succeeds after the source changes; a new stale comment fails.
    assert (await ctx.client.post(PATH + "/comments", json=body)).status_code == 200
    stale, _ = await comment(ctx, value, id="c" * 32)
    assert stale.status_code == 409
    current = await detail(ctx)
    invalid, _ = await comment(ctx, current, id="c" * 32, line=99)
    assert invalid.status_code == 422
    resolved = await ctx.client.put(
        PATH + "/comments/" + saved["id"], json={"resolved": True}
    )
    assert (
        resolved.status_code == 200 and (await summary(ctx))["comments"][0]["resolved"]
    )
    assert (
        await ctx.client.put(
            PATH + "/comments/" + saved["id"], json={"resolved": False}
        )
    ).status_code == 200
    assert not (await summary(ctx))["comments"][0]["resolved"]


async def test_fix_request_is_reviewable_and_uses_existing_delivery(ctx):
    (ctx.repo / "greeting.py").write_text("wrong\n")
    value = await detail(ctx)
    await comment(ctx, value)
    state = await summary(ctx)
    request = {"version": state["version"], "comment_ids": ["b" * 32]}
    prepared = await ctx.client.post(PATH + "/fix-request", json=request)
    assert prepared.status_code == 200
    prompt = prepared.json()["prompt"]
    assert "greeting.py" in prompt and "wrong" in prompt and "Unicode" in prompt
    ctx.handle.execute_update.assert_not_awaited()
    message = {
        "id": "d" * 32,
        "expected_turn": 2,
        "prompt": prompt,
        "mode": "change",
        "profile_id": "demo",
        "max_steps": 20,
    }
    for _ in range(2):
        sent = await ctx.client.post(f"/api/tasks/{TASK}/messages", json=message)
        assert sent.status_code == 200 and sent.json()["status"] == "accepted"
    ctx.handle.execute_update.assert_awaited_once()
    assert ctx.handle.execute_update.call_args.args[1].payload["prompt"] == prompt
    assert not (await summary(ctx))["comments"][0]["resolved"]
    await ctx.client.put(PATH + "/comments/" + "b" * 32, json={"resolved": True})
    assert (
        await ctx.client.post(PATH + "/fix-request", json=request)
    ).status_code == 409
    request["version"] = (await summary(ctx))["version"]
    assert (
        await ctx.client.post(PATH + "/fix-request", json=request)
    ).status_code == 409


async def test_added_deleted_renamed_unicode_and_no_final_newline(ctx):
    (ctx.repo / "greeting.py").rename(ctx.repo / "new [é].py")
    (ctx.repo / "new [é].py").write_text("new text")
    values = {row["path"]: row for row in (await summary(ctx))["files"]}
    assert values["greeting.py"]["status"] == "deleted"
    assert values["new [é].py"]["status"] == "added"
    deleted = await detail(ctx)
    assert all(row["new"] == 0 for row in deleted["lines"])
    response, _ = await comment(ctx, deleted, side="old")
    assert response.status_code == 200
    added = await detail(ctx, "new [é].py")
    assert added["new_exists"] and not added["new_final_newline"]
    assert added["lines"][-1] == {
        "kind": "addition",
        "text": "new text",
        "old": 0,
        "new": 1,
    }


@pytest.mark.parametrize(
    "path", ["../outside", ".env", "private.key", "secrets.json", ".git/config"]
)
async def test_review_never_reads_excluded_paths(ctx, path):
    response = await ctx.client.get(PATH + "/file", params={"path": path})
    assert response.status_code == 400


async def test_unsupported_files_are_visible_and_cannot_be_marked_reviewed(ctx):
    (ctx.repo / "large.txt").write_text("a" * 100001)
    (ctx.repo / "binary.bin").write_bytes(b"\0private")
    (ctx.repo / "latin.txt").write_bytes(b"\xff")
    (ctx.repo / "link").symlink_to(ctx.root / "session-token")
    (ctx.repo / ".env").write_text("never-disclose-this")
    (ctx.repo / ".gitignore").write_text("ignored.txt\n")
    (ctx.repo / "ignored.txt").write_text("never-read-this")
    data = await summary(ctx)
    assert "never-disclose" not in str(data) and "never-read" not in str(data)
    rows = {r["path"]: r for r in data["files"]}
    assert ".env" not in rows and "ignored.txt" not in rows
    for name in ["large.txt", "binary.bin", "latin.txt", "link"]:
        assert rows[name]["issue"] and not rows[name]["reviewed"]
        marked = await ctx.client.put(
            PATH + "/checkpoint", json={"path": name, "token": rows[name]["token"]}
        )
        assert marked.status_code in {400, 422}


async def test_task_base_remains_stable_after_local_commit(ctx):
    (ctx.repo / "greeting.py").write_text("committed change\n")
    git(ctx.repo, "add", "greeting.py")
    git(
        ctx.repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "Local commit",
    )
    assert (await summary(ctx))["files"][0]["status"] == "modified"
    assert (await detail(ctx))["lines"][-1]["text"] == "committed change"
    exported = await ctx.client.get(f"/api/tasks/{TASK}/patch")
    assert exported.status_code == 200 and "+committed change" in exported.text


async def test_mode_changes_and_deletion_after_review(ctx):
    (ctx.repo / "greeting.py").chmod(0o755)
    value = await detail(ctx)
    assert (
        value["old_mode"] == "100644"
        and value["new_mode"] == "100755"
        and value["changed"]
    )
    await ctx.client.put(
        PATH + "/checkpoint",
        json={"path": value["path"], "token": value["source_token"]},
    )
    (ctx.repo / "greeting.py").unlink()
    assert (await summary(ctx))["files"][0]["since_review"]
    assert (await detail(ctx, comparison="reviewed"))["changed"]


async def test_review_deletion_cleans_checkpoints_and_comments(ctx):
    (ctx.repo / "greeting.py").write_text("change\n")
    await comment(ctx, await detail(ctx))
    ctx.store.delete_task(TASK)
    assert Store(ctx.root).review(TASK) == {
        "version": 0,
        "checkpoints": {},
        "comments": [],
    }
    assert (await ctx.client.get(PATH)).status_code == 400


def test_grouped_diff_has_correct_old_and_new_line_numbers():
    lines = reviews.diff_lines("one\ntwo\nthree\n", "one\ninserted\ntwo\n")
    assert [(r["kind"], r["old"], r["new"]) for r in lines[1:]] == [
        ("context", 1, 1),
        ("addition", 0, 2),
        ("context", 2, 3),
        ("deletion", 3, 0),
    ]
