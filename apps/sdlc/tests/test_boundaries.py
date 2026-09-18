import hashlib
import json
from pathlib import Path

import httpx
import pytest

from sdlc_builder.models import Action
from sdlc_builder.store import Store, workspace
from sdlc_builder.workspaces import (
    continue_workspace,
    demo_project,
    diff,
    git,
    perform,
    prepare_workspace,
    read_file,
    safe_path,
    write_file,
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def repo(root):
    source = demo_project()
    task_id = "a" * 32
    prepare_workspace(str(source), task_id)
    return workspace(task_id)


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/etc/passwd",
        ".git/config",
        ".env",
        ".env.local",
        "a/.env",
        "private.pem",
        "secrets.json",
    ],
)
def test_excluded_paths(repo, path):
    with pytest.raises(ValueError):
        safe_path(repo, path)


def test_symlink_and_ignored_inputs(repo, root):
    outside = root / "outside"
    outside.write_text("private")
    (repo / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        read_file(repo, "link")
    (repo / ".gitignore").write_text("private.txt\n")
    (repo / "private.txt").write_text("private")
    with pytest.raises(ValueError):
        read_file(repo, "private.txt")


def test_hash_conflict_and_retry(repo):
    original = read_file(repo, "greeting.py")
    write_file(repo, "greeting.py", "first\n", original["hash"])
    with pytest.raises(ValueError, match="changed since"):
        write_file(repo, "greeting.py", "second\n", original["hash"])
    # Lost activity completion after a successful atomic write is safe to retry.
    assert write_file(repo, "greeting.py", "first\n", original["hash"])["unchanged"]


def test_source_checkout_and_continuation(repo, root):
    source = demo_project()
    (source / "greeting.py").write_text("user's unfinished work")
    original = read_file(repo, "greeting.py")
    write_file(
        repo, "greeting.py", "agent change without final newline", original["hash"]
    )
    write_file(repo, "new file.txt", "new text", "")
    (repo / "test_greeting.py").unlink()
    changes = diff(repo)
    assert set(changes["files"]) == {"greeting.py", "test_greeting.py", "new file.txt"}
    assert "No newline at end of file" in changes["patch"]
    continued = continue_workspace(repo.name, "b" * 32)
    next_root = Path(continued["workspace"])
    assert (
        next_root / "greeting.py"
    ).read_text() == "agent change without final newline"
    assert (next_root / "new file.txt").read_text() == "new text"
    assert not (next_root / "test_greeting.py").exists()
    assert (source / "greeting.py").read_text() == "user's unfinished work"
    assert git(source, "branch", "--show-current").strip() == "main"


async def test_operation_journal_rejects_argument_reuse(repo):
    action = Action(
        kind="write", summary="new file", path="one.txt", content="once"
    ).model_dump()
    first = await perform(repo.name, "op-1", action)
    assert first == await perform(repo.name, "op-1", action)
    action["content"] = "twice"
    with pytest.raises(ValueError, match="reused"):
        await perform(repo.name, "op-1", action)


async def test_uncertain_command_is_not_reexecuted(repo, root):
    action = Action(
        kind="check", summary="check", argv=["python3", "-c", "print('repeated')"]
    ).model_dump()
    fingerprint = hashlib.sha256(
        json.dumps(action, sort_keys=True).encode()
    ).hexdigest()
    journal = root / "operations" / repo.name
    journal.mkdir(parents=True)
    (journal / "uncertain.json").write_text(json.dumps({"fingerprint": fingerprint}))
    result = await perform(repo.name, "uncertain", action)
    assert "unknown" in result["error"]


async def test_command_environment_excludes_provider_credentials(repo, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-secret")
    action = Action(
        kind="check",
        summary="inspect environment",
        argv=[
            "python3",
            "-c",
            "import os; print(os.environ.get('OPENAI_API_KEY','absent')); print(os.environ['HOME']); print(os.environ['PYTHONDONTWRITEBYTECODE'])",
        ],
    ).model_dump()
    result = await perform(repo.name, "env-check", action)
    assert result["exit_code"] == 0
    assert "fake-test-secret" not in result["output"]
    assert "absent" in result["output"] and "command-homes" in result["output"]
    assert result["output"].rstrip().endswith("1")


def test_archive_never_rolls_back_newer_state(root):
    store = Store()
    store.put("tasks", {"id": "a" * 32})
    store.archive("a" * 32, {"version": 5, "value": {"phase": "check"}})
    store.archive("a" * 32, {"version": 3, "value": {"phase": "build"}})
    assert Store().get("tasks", "a" * 32)["snapshot"]["value"]["phase"] == "check"


async def test_local_api_boundary_and_key_storage(root, monkeypatch):
    from sdlc_builder.server import create_app

    saved = {}
    import keyring

    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, identity, value: saved.update({identity: value}),
    )
    monkeypatch.setattr(
        keyring, "delete_password", lambda service, identity: saved.pop(identity)
    )
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (await client.get("/api/home")).status_code == 401
        token = (root / "session-token").read_text().strip()
        assert (
            await client.post("/api/unlock", json={"token": token})
        ).status_code == 403
        headers = {"X-SDLC": "1"}
        assert (
            await client.post(
                "/api/unlock",
                json={"token": token},
                headers={**headers, "Origin": "https://evil.example"},
            )
        ).status_code == 403
        assert (
            await client.post("/api/unlock", json={"token": token}, headers=headers)
        ).status_code == 200
        response = await client.post(
            "/api/profiles",
            headers=headers,
            json={
                "label": "Test",
                "provider": "openai",
                "model": "test-model",
                "api_key": "fake-provider-key",
            },
        )
        assert response.status_code == 200, response.text
        assert "fake-provider-key" not in response.text
        assert saved[response.json()["id"]] == "fake-provider-key"
        assert "fake-provider-key" not in json.dumps(Store().all("profiles"))
        invalid = await client.post(
            "/api/profiles",
            headers=headers,
            json={
                "label": "Test",
                "provider": "openai",
                "model": "test-model",
                "api_key": "fake-provider-key" * 300,
            },
        )
        assert invalid.status_code == 422 and "fake-provider-key" not in invalid.text
        assert (
            await client.delete(
                "/api/profiles/" + response.json()["id"], headers=headers
            )
        ).status_code == 200
        assert not saved
        assert (
            await client.post("/harness/api/chat", headers=headers, json={})
        ).status_code == 403
        assert (
            await client.get("/api/home", headers={"Host": "attacker.example"})
        ).status_code == 403
