# ABOUTME: End-to-end test of the Code Mode virtual filesystem with NO model in the loop. A
# model-free parent (CodeModeVfsE2EParentWorkflow) mounts an activity-seeded in-memory /skills,
# a writable in-memory /workspace and an activity-backed /remote, and runs scripts over them, so
# seeding, fs_* tool dispatch through run_tool, persistence across scripts, error mapping across
# the activity boundary, and replay are exercised under a WorkflowEnvironment.
#
# Run with: uv run pytest tests/examples/monty/test_code_mode_vfs_e2e.py -v

from __future__ import annotations

import uuid
from typing import Any

import pytest_asyncio
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from . import _code_mode_vfs_e2e_parent as parent
from ._code_mode_e2e_parent import add_tool
from ._code_mode_vfs_e2e_parent import VFS_ACTIVITIES, CodeModeVfsE2EParentWorkflow


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"code-mode-vfs-e2e-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CodeModeVfsE2EParentWorkflow],
        activities=VFS_ACTIVITIES,
        plugins=[AgentHarnessPlugin(tools=[add_tool])],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _start(client: Client, task_queue: str) -> WorkflowHandle[Any, Any]:
    return await client.start_workflow(
        CodeModeVfsE2EParentWorkflow.run,
        AgentConfig(),
        id=f"CodeModeVfsE2EParent-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def _send(
    client: Client, handle: WorkflowHandle[Any, Any], script: str
) -> tuple[str, list[AgentEvent]]:
    """Run one script and return its reply plus the events its message produced."""
    accepted = await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="run_code", payload={"script": script}),
        result_type=AgentMessageReply,
    )
    events: list[AgentEvent] = []
    stream = WorkflowStreamClient.create(client, handle.id)
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
    ):
        envelope: AgentEvent = item.data
        if envelope.message_id != accepted.message_id:
            continue
        events.append(envelope)
        if envelope.event.type == AgentEventType.MESSAGE_HANDLER_END:
            return envelope.event.output["text"], events
    raise AssertionError("the stream ended before the message's reply")


def _tool_names(events: list[AgentEvent]) -> list[str]:
    return [e.event.tool_name for e in events if e.event.type == AgentEventType.TOOL_START]


async def _replay(handle: WorkflowHandle[Any, Any]) -> None:
    history = await handle.fetch_history()
    await Replayer(
        workflows=[CodeModeVfsE2EParentWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(history)


async def test_a_script_reads_skills_seeded_by_an_activity(client_and_queue, monkeypatch):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    script = (
        "from pathlib import Path\n"
        "with open('/skills/pdf/SKILL.md') as f:\n"
        "    skill = f.read()\n"
        "[sorted(p.name for p in Path('/skills/pdf').iterdir()), skill.splitlines()[-1]]\n"
    )
    reply, events = await _send(client, handle, script)
    assert "result: [['SKILL.md', 'forms.md'], 'Use forms.md for forms.']" in reply

    # The seed ran once, before the first operation, and every backend call went through
    # run_tool as an fs_* tool.
    names = _tool_names(events)
    assert names[0] == "run_code"
    assert names[1] == "fs_seed"
    assert names.count("fs_seed") == 1
    assert {"fs_stat", "fs_read", "fs_list"} <= set(names)
    seed_start = next(
        e for e in events if e.event.type == AgentEventType.TOOL_START and e.event.tool_name == "fs_seed"
    )
    assert seed_start.event.tool_input == {"mount": "/skills"}

    # A deploy that changes the skill files does not change what this workflow seeded: replay
    # reads the seed's result from history.
    monkeypatch.setattr(parent, "SKILL_FILES", {"other/SKILL.md": "changed"})
    await _replay(handle)


async def test_workspace_files_persist_across_scripts_and_the_seed_runs_once(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    write = (
        "import asyncio\n"
        "from pathlib import Path\n"
        "async def main():\n"
        "    forms = Path('/skills/pdf/forms.md').read_text()\n"
        "    total = await add({'a': 2, 'b': 3})\n"
        "    Path('/workspace/notes.txt').write_text(forms + str(total['total']))\n"
        "    return 'ok'\n"
        "asyncio.run(main())\n"
    )
    reply, first = await _send(client, handle, write)
    assert "result: 'ok'" in reply, reply

    read = (
        "from pathlib import Path\n"
        "[Path('/workspace/notes.txt').read_text(), Path('/skills/pdf/SKILL.md').exists()]\n"
    )
    reply, second = await _send(client, handle, read)
    assert "result: ['Fill every field.\\n5', True]" in reply, reply
    assert _tool_names(first).count("fs_seed") == 1
    assert "fs_seed" not in _tool_names(second)
    await _replay(handle)


async def test_a_tracked_workspace_publishes_its_changes_as_state_patches(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    script = (
        "from pathlib import Path\n"
        "Path('/workspace/out').mkdir()\n"
        "Path('/workspace/out/report.md').write_text('# Report')\n"
        "Path('/workspace/out/report.md').read_text()\n"
    )
    reply, events = await _send(client, handle, script)
    assert "result: '# Report'" in reply, reply

    patches = [
        e.event
        for e in events
        if e.event.type == AgentEventType.STATE_PATCH and e.event.state_id == "workspace"
    ]
    assert [[(op["op"], op["path"]) for op in p.ops] for p in patches] == [
        [("add", "/directories/-")],
        [("add", "/files/out~1report.md")],
    ]
    assert patches[1].ops[0]["value"] == {"content": "# Report", "encoding": "utf-8", "size": 8}
    await _replay(handle)


async def test_an_untouched_mount_is_never_seeded(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    reply, events = await _send(
        client, handle, "from pathlib import Path\nPath('/workspace').is_dir()\n"
    )
    assert "result: True" in reply
    assert "fs_seed" not in _tool_names(events)


async def test_an_activity_backed_mount_and_its_errors(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    script = (
        "from pathlib import Path\n"
        "report = Path('/remote/report.txt').read_text()\n"
        "try:\n"
        "    Path('/remote/ghost.txt').read_text()\n"
        "    ghost = 'read'\n"
        "except FileNotFoundError as e:\n"
        "    ghost = 'missing: ' + str(e)\n"
        "try:\n"
        "    Path('/remote/new.txt').write_text('x')\n"
        "    write = 'written'\n"
        "except PermissionError:\n"
        "    write = 'read-only'\n"
        "[report, ghost, write]\n"
    )
    reply, _ = await _send(client, handle, script)
    assert "'Q3 revenue: 42\\n'" in reply, reply
    # The activity's ApplicationError(type="FileNotFoundError") reached the script as the
    # built-in exception, catchable by name.
    assert "missing: No such file or directory: 'ghost.txt'" in reply, reply
    assert "'read-only'" in reply
    await _replay(handle)


async def test_a_host_call_made_before_a_file_operation_still_resolves(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    script = (
        "import asyncio\n"
        "from pathlib import Path\n"
        "async def main():\n"
        "    pending = add({'a': 1, 'b': 1})\n"
        "    report = Path('/remote/report.txt').read_text()\n"
        "    total = await pending\n"
        "    return [report.split(': ')[1].strip(), total['total']]\n"
        "asyncio.run(main())\n"
    )
    reply, events = await _send(client, handle, script)
    assert "result: ['42', 2]" in reply, reply
    names = _tool_names(events)
    # The add call was handed over when the script stopped at its first file operation, so it
    # started before the remote read did.
    assert names.index("add") < names.index("fs_read")
    await _replay(handle)
