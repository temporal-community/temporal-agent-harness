"""V2 authority, receipts and resource limits, including failure/recovery paths."""

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from temporal_agent_harness.harness.state import StateRef

from sdlc_builder import workspaces as ws
from sdlc_builder.bounded_code import bounded_resume, bounded_start
from sdlc_builder.coding_models import (
    Actor,
    CodingState,
    EditRequest,
    FileChange,
    PatchRequest,
    ToolCall,
    ToolResult,
)
from sdlc_builder.coding_workflow import CodingWorkflow
from sdlc_builder.coding_workspace import coding_operation, revision
from sdlc_builder.models import ConversationState, ConversationTurn, Profile, TaskInput


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_DATA_DIR", str(tmp_path))
    project = ws.demo_project()
    task_id = "b" * 32
    root = Path(ws.prepare_workspace(str(project), task_id)["workspace"])
    return task_id, root


async def test_patch_preflight_and_crash_recovery(repo, monkeypatch):
    task, root = repo
    old = ws.read_file(root, "greeting.py")
    changes = [
        FileChange(path="greeting.py", expected_hash=old["hash"], content="replaced\n"),
        FileChange(path="note.txt", expected_hash="stale", content="note\n"),
    ]
    failed = await coding_operation(
        task, "conflict", ToolCall(kind="patch", changes=changes)
    )
    assert not failed.ok and ws.read_file(root, "greeting.py") == old
    changes[1].expected_hash = ""
    write = ws.write_file
    crash = True

    def interrupted(*args):
        nonlocal crash
        result = write(*args)
        if crash:
            crash = False
            raise RuntimeError("worker crashed after replace")
        return result

    monkeypatch.setattr(ws, "write_file", interrupted)
    call = ToolCall(kind="patch", changes=changes)
    with pytest.raises(RuntimeError):
        await coding_operation(task, "recover", call)
    result = await coding_operation(task, "recover", call)
    assert result.ok and (root / "note.txt").read_text() == "note\n"
    assert await coding_operation(task, "recover", call) == result
    mismatch = await coding_operation(task, "recover", ToolCall(kind="revision"))
    assert not mismatch.ok


async def test_exact_edit_uncertain_check_and_source_bound_evidence(repo, monkeypatch):
    task, root = repo
    current = ws.read_file(root, "greeting.py")
    call = ToolCall(
        kind="edit",
        edit=EditRequest(
            path="greeting.py",
            expected_hash=current["hash"],
            old_text="hello",
            new_text="Hello",
        ),
    )
    result = await coding_operation(task, "edit", call)
    assert result.ok and result.before_revision != result.revision
    record = root.parents[1] / "coding-operations" / task / "edit.json"
    prior = json.loads(record.read_text())
    del prior["result"]
    record.write_text(json.dumps(prior))
    assert (await coding_operation(task, "edit", call)).ok
    expected = revision(root)
    command = ToolCall(
        kind="check",
        argv=[
            "python3",
            "-c",
            "from pathlib import Path; Path('generated.txt').write_text('new')",
        ],
        expected_revision=expected,
    )
    checked = await coding_operation(task, "check", command)
    assert checked.exit_code == 0 and checked.before_revision == expected
    assert checked.revision != expected  # Passing but mutated => not verified.
    assert await coding_operation(task, "check", command) == checked
    changed = await coding_operation(task, "changed", command)
    assert not changed.ok and "changed after" in changed.error
    uncertain = root.parents[1] / "coding-operations" / task / "unknown.json"
    uncertain.write_text(
        json.dumps(
            {
                "fingerprint": hashlib.sha256(
                    command.model_dump_json().encode()
                ).hexdigest()
            }
        )
    )
    execute = AsyncMock()
    monkeypatch.setattr(ws, "execute_check", execute)
    assert "not run twice" in (await coding_operation(task, "unknown", command)).error
    execute.assert_not_called()


def controller(mode="change", role="implementer"):
    obj = CodingWorkflow.__new__(CodingWorkflow)
    profile = Profile(id="test", label="test", provider="demo", max_steps=3)
    obj.current_task = TaskInput(
        task_id="b" * 32, prompt="Test", mode=mode, profile=profile
    )
    obj.state = StateRef("sdlc", CodingState(actors=[Actor(id="role", role=role)]))
    obj.conversation = StateRef(
        "conversation",
        ConversationState(
            turns=[
                ConversationTurn(
                    number=1,
                    message_id="initial",
                    prompt="Test",
                    mode=mode,
                    profile_id="test",
                    profile_label="test",
                    provider="demo",
                    model="demo",
                    max_steps=3,
                )
            ]
        ),
    )
    obj.active_actor, obj.active_role, obj.role_limit = "role", role, 200
    obj.approved_scope, obj.host_count = ["src/"], 0
    obj.lock = asyncio.Lock()
    obj.checkpoint = AsyncMock()
    obj.operation = AsyncMock(return_value=ToolResult(operation_id="receipt"))
    return obj


@pytest.mark.parametrize(
    "mode,role,path",
    [
        ("ask", "implementer", "src/a.py"),
        ("change", "reviewer", "src/a.py"),
        ("change", "coordinator", "src/a.py"),
        ("change", "implementer", "outside.py"),
        ("change", "implementer", "src/../outside.py"),
    ],
)
async def test_controller_refuses_writes_and_commands_without_role_and_scope(
    mode, role, path
):
    obj = controller(mode, role)
    request = PatchRequest(
        changes=[FileChange(path=path, expected_hash="", content="blocked")]
    )
    assert not (
        await obj.dispatch(
            ToolCall(
                kind="edit",
                edit=EditRequest(
                    path=path, expected_hash="", old_text="before", new_text="after"
                ),
            )
        )
    ).ok
    # Native and scripted tools both rendezvous at this controller boundary.
    assert not (await obj.dispatch(ToolCall(kind="patch", changes=request.changes))).ok
    assert not (await obj.dispatch(ToolCall(kind="check", argv=["sh"]))).ok
    obj.operation.assert_not_called()


async def test_aggregate_budget_and_stopped_host_calls():
    obj = controller()
    for role in ["coordinator", "implementer", "reviewer"]:
        obj.active_role = role
        assert (await obj.dispatch(ToolCall(kind="model"))).ok
    assert not (await obj.dispatch(ToolCall(kind="model"))).ok
    assert obj.state.current.model_calls == 3
    obj.checkpoint.side_effect = InterruptedError("Stopped")
    with pytest.raises(InterruptedError):
        await obj.dispatch(ToolCall(kind="read", path="src/a.py"))
    obj.operation.assert_not_called()


async def test_controller_waits_for_settling_script_operation():
    obj = controller()
    await obj.lock.acquire()
    pending = asyncio.create_task(obj.controller_operation(ToolCall(kind="revision")))
    await asyncio.sleep(0)
    obj.operation.assert_not_called()
    obj.lock.release()
    await pending
    obj.operation.assert_awaited_once()


async def test_code_mode_limits_and_durable_resume():
    from temporal_agent_harness.harness.code_mode.batch_models import (
        CallResult,
        ResumeBatchInput,
    )

    first = await bounded_start('value = await read_file("a.py")\nvalue')
    assert not first.done and len(first.pending) == 1
    resumed = await bounded_resume(
        ResumeBatchInput(
            snapshot=first.snapshot,
            results=[
                CallResult(call_id=first.pending[0].call_id, return_value="fixture")
            ],
        )
    )
    assert resumed.output_json == '"fixture"'
    runaway = await bounded_start("while True:\n    pass")
    assert runaway.done and "limit" in runaway.error
    huge = await bounded_start("[0] * 100_000_000")
    assert huge.done and "memory limit" in huge.error
    fanout = await bounded_start(
        'import asyncio\nawait asyncio.gather(*[read_file("a.py") for _ in range(9)])'
    )
    assert fanout.done and "8 host calls" in fanout.error
    assert "20,000" in (await bounded_start(" " * 20001)).error
