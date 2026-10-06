# ABOUTME: Tests for the Code Mode memory example: LocalDisk's activities work on a directory
# inside memories/ and refuse configs and paths that reach outside it, and, with no model in the
# loop, two separate sessions given the same memory_dir share their memories through the
# activity-backed, indexed /memory mount.
#
# Run with: uv run pytest tests/examples/code_mode_memory -v

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from examples.code_mode_memory import local_disk
from examples.code_mode_memory.activities import load_skills
from examples.code_mode_memory.local_disk import LocalDisk
from examples.code_mode_memory.workflow import MemoryConfig
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
)
from temporal_agent_harness.harness.code_mode.activity_fs import (
    FsCall,
    FsRead,
    FsRename,
    FsWrite,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from ._memory_e2e_parent import CodeModeMemoryE2EParentWorkflow

DISK = {a.__temporal_activity_definition.name: a for a in LocalDisk.activities}


@pytest.fixture
def memories(tmp_path, monkeypatch):
    """A fresh memories/ folder, with a file beside it that no config may reach."""
    folder = tmp_path / "memories"
    folder.mkdir()
    (tmp_path / "outside.md").write_text("not memory")
    monkeypatch.setattr(local_disk, "MEMORIES_DIR", folder)
    return folder


# ---------------------------------------------------------------- the activities


async def test_load_skills_ships_the_memory_skill():
    files = await ActivityEnvironment().run(load_skills)
    assert set(files) == {"memory/SKILL.md", "memory/template.md"}
    assert files["memory/SKILL.md"].startswith("---\nname: memory\n")


async def test_local_disk_works_on_a_directory_inside_memories(memories):
    env = ActivityEnvironment()
    alice = {"directory": "alice"}

    # The directory is created on first use.
    root_stat = await env.run(DISK["vfs.local-disk.stat"], FsCall(config=alice, path="."))
    assert root_stat.found is not None and root_stat.found.is_dir
    assert (memories / "alice").is_dir()

    await env.run(DISK["vfs.local-disk.mkdir"], FsCall(config=alice, path="topics"))
    await env.run(
        DISK["vfs.local-disk.write"],
        FsWrite(config=alice, path="topics/coffee.md", content="flat white\n"),
    )
    assert (memories / "alice" / "topics" / "coffee.md").read_text() == "flat white\n"
    page = await env.run(
        DISK["vfs.local-disk.view"], FsRead(config=alice, path="topics/coffee.md", offset=5)
    )
    assert (page.content, page.size) == ("white\n", 11)

    await env.run(
        DISK["vfs.local-disk.rename"], FsRename(config=alice, path="topics", target="kept")
    )
    names = await env.run(DISK["vfs.local-disk.list"], FsCall(config=alice, path="kept"))
    assert names == ["coffee.md"]
    await env.run(DISK["vfs.local-disk.delete"], FsCall(config=alice, path="kept/coffee.md"))
    await env.run(DISK["vfs.local-disk.delete"], FsCall(config=alice, path="kept"))
    assert await env.run(DISK["vfs.local-disk.list"], FsCall(config=alice, path=".")) == []


@pytest.mark.parametrize(
    ("directory", "path", "error_type"),
    [
        ("/etc", "passwd", "ValueError"),  # absolute
        ("../..", "outside.md", "ValueError"),  # climbs out of memories/
        ("alice/../..", "outside.md", "ValueError"),
        (".", "outside.md", "ValueError"),  # memories/ itself, not a directory inside it
        ("alice", "../../outside.md", "PermissionError"),  # a path that climbs out
        ("alice", "missing.md", "FileNotFoundError"),
    ],
)
async def test_local_disk_refuses_anything_outside_its_directory(
    memories, directory, path, error_type
):
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(
            DISK["vfs.local-disk.view"], FsRead(config={"directory": directory}, path=path)
        )
    assert raised.value.type == error_type
    assert raised.value.non_retryable


async def test_local_disk_keeps_its_directory(memories):
    env = ActivityEnvironment()
    for operation in ("vfs.local-disk.delete", "vfs.local-disk.rename"):
        call = (
            FsRename(config={"directory": "alice"}, path=".", target="moved")
            if operation.endswith("rename")
            else FsCall(config={"directory": "alice"}, path=".")
        )
        with pytest.raises(ApplicationError) as raised:
            await env.run(DISK[operation], call)
        assert raised.value.type == "PermissionError"
    assert (memories / "alice").is_dir()

# ---------------------------------------------------------------- two sessions, one memory

# What a model following skills/memory/SKILL.md would run to save a memory...
_SAVE = """
from pathlib import Path
skill = Path('/skills/memory/SKILL.md').read_text()
index = Path('/memory/MEMORY.md')
coffee = '# Favorite coffee\\n\\nA flat white with oat milk.\\n'
Path('/memory/favorite-coffee.md').write_text(coffee)
before = index.read_text() if index.exists() else '# Memory\\n'
index.write_text(before + '- [Favorite coffee](favorite-coffee.md) — flat white, oat milk\\n')
sorted(p.name for p in Path('/memory').iterdir())
"""

# ...and to recall it, in another session.
_RECALL = """
from pathlib import Path
index = Path('/memory/MEMORY.md').read_text()
[index.splitlines()[-1], Path('/memory/favorite-coffee.md').read_text().splitlines()[2]]
"""


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"code-mode-memory-e2e-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CodeModeMemoryE2EParentWorkflow],
        activities=[load_skills],
        plugins=[AgentHarnessPlugin(filesystems=[LocalDisk])],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _start(client: Client, task_queue: str, memory_dir: str) -> WorkflowHandle[Any, Any]:
    return await client.start_workflow(
        CodeModeMemoryE2EParentWorkflow.run,
        args=[AgentConfig(), MemoryConfig(memory_dir=memory_dir)],
        id=f"CodeModeMemoryE2EParent-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def _send(client: Client, handle: WorkflowHandle[Any, Any], script: str) -> str:
    """Run one script and return its reply."""
    accepted = await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="run_code", payload={"script": script}),
        result_type=AgentMessageReply,
    )
    stream = WorkflowStreamClient.create(client, handle.id)
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
    ):
        envelope: AgentEvent = item.data
        if (
            envelope.message_id == accepted.message_id
            and envelope.event.type == AgentEventType.MESSAGE_HANDLER_END
        ):
            return envelope.event.output["text"]
    raise AssertionError("the stream ended before the message's reply")


async def _replay(handle: WorkflowHandle[Any, Any]) -> None:
    history = await handle.fetch_history()
    await Replayer(
        workflows=[CodeModeMemoryE2EParentWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(history)


async def test_a_second_session_recalls_what_the_first_one_saved(client_and_queue, memories):
    client, task_queue = client_and_queue
    memory_dir = "alice"

    first = await _start(client, task_queue, memory_dir)
    reply = await _send(client, first, _SAVE)
    assert "result: ['MEMORY.md', 'favorite-coffee.md']" in reply
    assert (memories / "alice" / "favorite-coffee.md").exists()

    second = await _start(client, task_queue, memory_dir)
    reply = await _send(client, second, _RECALL)
    assert (
        "result: ['- [Favorite coffee](favorite-coffee.md) — flat white, oat milk', "
        "'A flat white with oat milk.']"
    ) in reply

    # A session with another memory_dir sees none of it.
    stranger = await _start(client, task_queue, "bob")
    script = "from pathlib import Path\nPath('/memory/MEMORY.md').exists()"
    reply = await _send(client, stranger, script)
    assert "result: False" in reply

    # Every file operation was recorded, so the sessions replay without the disk.
    # The second session's index holds what it saw, and says where to read the files.
    index = await second.query(CodeModeMemoryE2EParentWorkflow.index)
    assert set(index["entries"]) == {"MEMORY.md", "favorite-coffee.md"}
    assert index["source"]["filesystem"] == "local-disk"
    assert index["source"]["task_queue"] == task_queue

    for path in (memories / "alice").iterdir():
        path.unlink()
    await _replay(first)
    await _replay(second)


async def test_a_disk_failure_reaches_the_script_as_the_builtin(client_and_queue, memories):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue, "alice")
    reply = await _send(
        client,
        handle,
        "try:\n"
        "    open('/memory/no-such-dir/note.md', 'w')\n"
        "    outcome = 'opened'\n"
        "except FileNotFoundError as e:\n"
        "    outcome = 'FileNotFoundError'\n"
        "outcome\n",
    )
    assert "result: 'FileNotFoundError'" in reply
