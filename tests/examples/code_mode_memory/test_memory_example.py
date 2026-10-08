# ABOUTME: Tests for the Code Mode memory example: LocalDisk's activities work on a directory
# inside memories/ and refuse configs and paths that reach outside it, SkillsDisk serves the
# skills folder read-only, and, with no model in the loop, two separate sessions given the same
# memory_dir share their memories through the /memory OKF bundle, and the agent reads that
# bundle's index.md for its system instruction.
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
from examples.code_mode_memory.local_disk import LocalDisk, SkillsDisk
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
SKILLS = {a.__temporal_activity_definition.name: a for a in SkillsDisk.activities}


@pytest.fixture
def memories(tmp_path, monkeypatch):
    """A fresh memories/ folder, with a file beside it that no config may reach."""
    folder = tmp_path / "memories"
    folder.mkdir()
    (tmp_path / "outside.md").write_text("not memory")
    monkeypatch.setattr(local_disk, "MEMORIES_DIR", folder)
    return folder


# ---------------------------------------------------------------- the activities


async def test_skills_disk_serves_the_skills_folder_read_only():
    env = ActivityEnvironment()
    assert await env.run(SKILLS["vfs.skills-disk.list"], FsCall(config={}, path=".")) == [
        "memory"
    ]
    page = await env.run(SKILLS["vfs.skills-disk.view"], FsRead(config={}, path="memory/SKILL.md"))
    assert page.content.startswith("---\nname: memory\n")
    assert not any(name.endswith((".write", ".delete", ".rename", ".mkdir")) for name in SKILLS)
    with pytest.raises(ApplicationError) as raised:
        await env.run(SKILLS["vfs.skills-disk.view"], FsRead(config={}, path="../local_disk.py"))
    assert raised.value.type == "PermissionError"


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
import asyncio
from pathlib import Path

async def main():
    Path('/skills/memory/SKILL.md').read_text()
    known = await okf_concepts()
    Path('/memory/favorite-coffee.md').write_text(await okf_render(
        {'type': 'Preference', 'title': 'Favorite coffee',
         'description': 'A flat white with oat milk.', 'tags': ['coffee', 'drinks']},
        'A flat white with oat milk.'))
    Path('/memory/index.md').write_text(
        '# Preference\\n\\n* [Favorite coffee](favorite-coffee.md) - A flat white with oat milk.\\n')
    Path('/memory/log.md').write_text(
        '# Directory Update Log\\n\\n## 2026-10-06\\n* **Creation**: [Favorite coffee](favorite-coffee.md)\\n')
    return [len(known), sorted(p.name for p in Path('/memory').iterdir())]

asyncio.run(main())
"""

# ...and to recall it, in another session.
_RECALL = """
import asyncio
from pathlib import Path

async def main():
    concepts = await okf_concepts()
    coffee = [c for c in concepts if 'coffee' in c.get('tags', [])]
    return [[(c['title'], c['type'], c['description']) for c in coffee],
            Path(coffee[0]['path']).read_text().splitlines()[-1]]

asyncio.run(main())
"""


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"code-mode-memory-e2e-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CodeModeMemoryE2EParentWorkflow],
        plugins=[AgentHarnessPlugin(filesystems=[LocalDisk, SkillsDisk])],
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
    message = AgentMessage(type="run_code", payload={"script": script})
    return await _message(client, handle, message)


async def _message(client: Client, handle: WorkflowHandle[Any, Any], message: AgentMessage) -> str:
    """Send one message and return its reply."""
    accepted = await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE, message, result_type=AgentMessageReply
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
    assert "result: [0, ['favorite-coffee.md', 'index.md', 'log.md']]" in reply, reply
    saved = (memories / "alice" / "favorite-coffee.md").read_text()
    assert saved.startswith("---\ntype: Preference\ntitle: Favorite coffee\n")
    assert "  by: CodeModeMemoryE2EParent\n" in saved

    second = await _start(client, task_queue, memory_dir)
    reply = await _send(client, second, _RECALL)
    assert (
        "result: [[['Favorite coffee', 'Preference', 'A flat white with oat milk.']], "
        "'A flat white with oat milk.']"
    ) in reply, reply

    # A session with another memory_dir sees none of it.
    stranger = await _start(client, task_queue, "bob")
    script = "import asyncio\nasync def main():\n    return await okf_concepts()\nasyncio.run(main())"
    reply = await _send(client, stranger, script)
    assert "result: []" in reply, reply

    # Every file operation was recorded, so the sessions replay without the disk.
    # The second session's index holds the file it read, and says where to read the files.
    index = await second.query(CodeModeMemoryE2EParentWorkflow.index)
    assert set(index["entries"]) == {"favorite-coffee.md"}
    assert index["okf_version"] == "0.2"
    assert index["source"]["filesystem"] == "local-disk"
    assert index["source"]["task_queue"] == task_queue

    for path in (memories / "alice").iterdir():
        path.unlink()
    await _replay(first)
    await _replay(second)


async def test_the_system_instruction_index_is_read_from_the_memory(client_and_queue, memories):
    client, task_queue = client_and_queue
    read_index = AgentMessage(type="read_index", payload={})

    fresh = await _start(client, task_queue, "alice")
    assert await _message(client, fresh, read_index) == "(empty: nothing saved yet)"

    await _send(client, fresh, _SAVE)
    reply = await _message(client, fresh, read_index)
    assert reply == (
        "# Preference\n\n* [Favorite coffee](favorite-coffee.md) - A flat white with oat milk.\n"
    )

    for path in (memories / "alice").iterdir():
        path.unlink()
    await _replay(fresh)


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
