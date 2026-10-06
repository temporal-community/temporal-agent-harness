# ABOUTME: Tests for ActivityFileSystem: a subclass's async methods become vfs.<name>.* activities
# (validated at class definition), the activities build an instance from each call's config and
# map failures to the built-in error names, `view` pages through a file, and an agent mounting
# one runs every operation as an activity and records what it sees in its FileIndex.
#
# Run with: uv run pytest tests/harness/test_activity_filesystem.py -v

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_client import AgentClient
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
    MAX_VIEW_BYTES,
    FileChunk,
    FsCall,
    FsRead,
    FsWrite,
)
from temporal_agent_harness.harness.code_mode.vfs import IndexSource
from temporal_agent_harness.plugin import AgentHarnessPlugin

from _activity_fs_parent import (
    STORES,
    ActivityFsParentWorkflow,
    ReadOnlyStoreFileSystem,
    Store,
    StoreAgentData,
    StoreConfig,
    StoreFileSystem,
)


def _by_name(fs: type[agent.ActivityFileSystem[Any]]) -> dict[str, Any]:
    return {a.__temporal_activity_definition.name: a for a in fs.activities}


@pytest.fixture
def store():
    name = f"store-{uuid.uuid4()}"
    STORES[name] = Store(files={"notes/a.md": b"hello", "logo.bin": b"\x00\xff"}, dirs={"notes"})
    yield name
    del STORES[name]


# ---------------------------------------------------------------- the class


def test_the_activities_are_named_for_the_filesystem():
    assert sorted(_by_name(StoreFileSystem)) == [
        "vfs.test-store.delete",
        "vfs.test-store.list",
        "vfs.test-store.mkdir",
        "vfs.test-store.read",
        "vfs.test-store.rename",
        "vfs.test-store.stat",
        "vfs.test-store.view",
        "vfs.test-store.write",
    ]
    assert sorted(_by_name(ReadOnlyStoreFileSystem)) == [
        "vfs.test-store-ro.list",
        "vfs.test-store-ro.read",
        "vfs.test-store-ro.stat",
        "vfs.test-store-ro.view",
    ]


class _Config(BaseModel):
    x: int = 0


def test_a_filesystem_class_is_checked_when_it_is_defined():
    with pytest.raises(ValueError, match="must be lowercase"):

        class BadName(agent.ActivityFileSystem[_Config], name="Bad Name"):
            async def stat(self, path): ...
            async def read(self, path, offset=0, length=None): ...
            async def list(self, path): ...

    with pytest.raises(TypeError, match="does not implement list"):

        class NoList(agent.ActivityFileSystem[_Config], name="no-list"):
            async def stat(self, path): ...
            async def read(self, path, offset=0, length=None): ...

    with pytest.raises(TypeError, match="needs all four"):

        class HalfWritable(agent.ActivityFileSystem[_Config], name="half"):
            async def stat(self, path): ...
            async def read(self, path, offset=0, length=None): ...
            async def list(self, path): ...
            async def write(self, path, data): ...

    with pytest.raises(TypeError, match="pydantic config model"):

        class NoConfig(agent.ActivityFileSystem, name="no-config"):  # type: ignore[type-arg]
            async def stat(self, path): ...
            async def read(self, path, offset=0, length=None): ...
            async def list(self, path): ...


def test_a_read_only_filesystem_cannot_back_a_writable_mount():
    backend = ReadOnlyStoreFileSystem.backend(StoreConfig(store="any"))
    with pytest.raises(TypeError, match="is writable"):
        agent.VFSMount("/store", backend)
    agent.VFSMount("/store", backend, read_only=True)
    with pytest.raises(TypeError, match="declare the mount read_only=True"):
        agent.vfs_mount("/store", ReadOnlyStoreFileSystem, description="")
    agent.vfs_mount("/store", ReadOnlyStoreFileSystem, description="", read_only=True)


def test_plugin_registers_filesystem_activities_and_refuses_anything_else():
    plugin = AgentHarnessPlugin(filesystems=[StoreFileSystem])
    registered = plugin._append_activities([])
    names = {a.__temporal_activity_definition.name for a in registered}
    assert set(_by_name(StoreFileSystem)) <= names
    with pytest.raises(TypeError, match="not an ActivityFileSystem"):
        AgentHarnessPlugin(filesystems=[object])  # type: ignore[list-item]


# ---------------------------------------------------------------- the activities


async def test_activities_build_an_instance_from_each_calls_config(store):
    env = ActivityEnvironment()
    fs = _by_name(StoreFileSystem)
    config = {"store": store}

    await env.run(fs["vfs.test-store.write"], FsWrite(config=config, path="b.txt", content="hi"))
    assert STORES[store].files["b.txt"] == b"hi"
    found = await env.run(fs["vfs.test-store.stat"], FsCall(config=config, path="b.txt"))
    assert found.found == agent.FileStat(is_dir=False, size=2, mtime=1.5)
    binary = await env.run(fs["vfs.test-store.read"], FsRead(config=config, path="logo.bin"))
    assert (binary.content, binary.encoding) == ("AP8=", "base64")
    assert await env.run(fs["vfs.test-store.list"], FsCall(config=config, path=".")) == [
        "b.txt",
        "logo.bin",
        "notes",
    ]


@pytest.mark.parametrize(
    ("config", "path", "error_type"),
    [
        ({"store": "missing"}, "notes/a.md", "ValueError"),  # the instance refused the config
        ({"nope": 1}, "notes/a.md", "ValueError"),  # the config model refused it
        (None, "nothing.md", "FileNotFoundError"),
    ],
)
async def test_failures_are_non_retryable_and_named_for_the_builtin(
    store, config, path, error_type
):
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(
            _by_name(StoreFileSystem)["vfs.test-store.read"],
            FsRead(config=config or {"store": store}, path=path),
        )
    assert raised.value.type == error_type
    assert raised.value.non_retryable


async def test_view_pages_through_a_file(store):
    STORES[store].files["big.txt"] = b"x" * (MAX_VIEW_BYTES + 10)
    view = _by_name(StoreFileSystem)["vfs.test-store.view"]
    env = ActivityEnvironment()

    first: FileChunk = await env.run(view, FsRead(config={"store": store}, path="big.txt"))
    assert (first.offset, len(first.content)) == (0, MAX_VIEW_BYTES)
    assert first.size == MAX_VIEW_BYTES + 10
    rest: FileChunk = await env.run(
        view, FsRead(config={"store": store}, path="big.txt", offset=MAX_VIEW_BYTES, length=10**9)
    )
    assert (rest.offset, rest.content, rest.mtime) == (MAX_VIEW_BYTES, "x" * 10, 1.5)

    with pytest.raises(ApplicationError) as raised:
        await env.run(view, FsRead(config={"store": store}, path="notes"))
    assert raised.value.type == "IsADirectoryError"


# ---------------------------------------------------------------- an agent mounting one


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"activity-fs-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[ActivityFsParentWorkflow],
        plugins=[AgentHarnessPlugin(filesystems=[StoreFileSystem])],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _send(client: Client, handle: WorkflowHandle[Any, Any], script: str) -> str:
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


async def test_a_mounted_activity_filesystem_is_indexed(client_and_queue, store):
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        ActivityFsParentWorkflow.run,
        args=[AgentConfig(), StoreAgentData(store=store)],
        id=f"ActivityFsParent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    script = (
        "from pathlib import Path\n"
        "names = sorted(p.name for p in Path('/store').iterdir())\n"
        "Path('/store/out').mkdir()\n"
        "Path('/store/out/r.md').write_text('# R')\n"
        "Path('/store/logo.bin').unlink()\n"
        "[names, Path('/store/notes/a.md').read_text()]\n"
    )
    reply = await _send(client, handle, script)
    assert "result: [['logo.bin', 'notes'], 'hello']" in reply, reply
    assert STORES[store].files["out/r.md"] == b"# R"

    index = await handle.query(ActivityFsParentWorkflow.index)
    assert index["kind"] == "file_index"
    assert (index["mount"], index["description"], index["read_only"]) == (
        "/store",
        "The test store.",
        False,
    )
    assert index["source"] == {
        "filesystem": "test-store",
        "config": {"store": store},
        "task_queue": task_queue,
    }
    entries = index["entries"]
    assert set(entries) == {"notes", "notes/a.md", "out", "out/r.md"}
    assert entries["notes"]["is_dir"] is True
    assert entries["notes/a.md"] == {"is_dir": False, "size": 5, "mtime": 1.5, "writes": 0}
    assert entries["out/r.md"] == {"is_dir": False, "size": 3, "mtime": None, "writes": 1}

    await Replayer(
        workflows=[ActivityFsParentWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(await handle.fetch_history())


async def _first_turn(client: Client, handle: WorkflowHandle[Any, Any]) -> list[AgentEvent]:
    """Every event the agent published up to the end of its first turn."""
    stream = WorkflowStreamClient.create(client, handle.id)
    events: list[AgentEvent] = []
    async with asyncio.timeout(10):
        async for item in stream.subscribe(
            topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
        ):
            events.append(item.data)
            if item.data.event.type == AgentEventType.TURN_END:
                break
    return events


async def test_a_new_session_with_a_declared_mount_publishes_only_its_snapshot(
    client_and_queue, store
):
    """Startup publishes a snapshot per declared state and nothing else, so attaching to a
    brand-new session finishes. A turn-0 patch would leave the attach open forever, which in
    the console kept a new session's message box disabled."""
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        ActivityFsParentWorkflow.run,
        args=[AgentConfig(), StoreAgentData(store=store)],
        id=f"ActivityFsParent-{uuid.uuid4()}",
        task_queue=task_queue,
    )

    stream = await AgentClient(client, handle.id).attach(
        from_offset=0, on_item=lambda item, _resume_offset: item
    )
    items: list[AgentEvent] = []
    async with asyncio.timeout(10):
        async for item in stream:
            items.append(item)

    assert [(i.event.type, i.turn_number) for i in items] == [
        (AgentEventType.STATE_SNAPSHOT, 0)
    ]
    snapshot = items[0].event
    assert snapshot.state_id == "files"
    assert snapshot.value == {
        "kind": "file_index",
        "mount": "/store",
        "description": "The test store.",
        "read_only": False,
        "source": None,
        "entries": {},
    }

    await _send(client, handle, "from pathlib import Path\nPath('/store/notes').is_dir()\n")
    patches = [
        e.event
        for e in await _first_turn(client, handle)
        if e.event.type == AgentEventType.STATE_PATCH
    ]
    # Where the files can be read arrives with the first file the index records.
    assert {op["path"] for op in patches[0].ops} == {"/source", "/entries/notes"}
    assert all(op["path"] != "/source" for p in patches[1:] for op in p.ops)


def test_backend_takes_only_its_own_config():
    with pytest.raises(TypeError, match="takes a StoreConfig"):
        StoreFileSystem.backend(_Config())  # type: ignore[arg-type]



class _NestedConfig(BaseModel):
    """A config with nested values, which the index carries as JSON like any other."""

    bucket: str
    auth: dict[str, list[str]]


class _Nested(agent.ActivityFileSystem[_NestedConfig], name="test-nested"):
    async def stat(self, path): ...
    async def read(self, path, offset=0, length=None): ...
    async def list(self, path): ...


def test_an_index_source_carries_a_nested_config():
    config = _NestedConfig(bucket="b", auth={"scopes": ["read", "list"]})
    source = IndexSource(
        filesystem="test-nested",
        config=_Nested.backend(config).source.config,
        task_queue="q",
    )
    assert source.config == {"bucket": "b", "auth": {"scopes": ["read", "list"]}}
    assert _NestedConfig.model_validate(source.config) == config
