# ABOUTME: Tests for agent.vfs_mount: a mount declared on the agent class is observable state
# whose starting value is the mount's details, and binding it per instance returns the VFSMount
# a code_mode_tool takes without writing anything to that state.

from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from pydantic import BaseModel

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.code_mode.vfs import IndexedVFSMount
from temporal_agent_harness.harness.state import StateEvent, declared_states


class _Config(BaseModel):
    store: str


class _Store(agent.ActivityFileSystem[_Config], name="test-decl-store"):
    async def stat(self, path): ...
    async def read(self, path, offset=0, length=None): ...
    async def list(self, path): ...
    async def write(self, path, data): ...
    async def mkdir(self, path): ...
    async def delete(self, path): ...
    async def rename(self, path, target): ...


class _Other(BaseModel):
    x: int = 0


class Agent:
    skills = agent.vfs_mount(
        "/skills", agent.InMemoryFileSystem, read_only=True, description="Skills."
    )
    store = agent.vfs_mount("/store", _Store, description="The store.")


def test_a_declared_mount_is_declared_state_starting_with_the_mounts_details():
    states = declared_states(Agent)
    assert list(states) == ["skills", "store"]
    assert states["skills"].state_type is agent.FileTree
    assert states["store"].state_type is agent.FileIndex

    instance = Agent()
    tree = instance.skills.state.current
    assert (tree.kind, tree.mount, tree.description, tree.read_only) == (
        "file_tree",
        "/skills",
        "Skills.",
        True,
    )
    assert (tree.files, tree.directories) == ({}, [])
    index = instance.store.state.current
    assert (index.kind, index.mount, index.description, index.read_only) == (
        "file_index",
        "/store",
        "The store.",
        False,
    )
    assert (index.source, index.entries) == (None, {})


def test_binding_writes_nothing_to_the_state():
    instance = Agent()
    published: list[StateEvent] = []
    for ref in (instance.skills.state, instance.store.state):
        ref._attach(published.append)  # as the runner does
    published.clear()

    async def seed() -> dict[str, str | bytes]:
        return {"a.md": "a"}

    agent.code_mode_tool(
        [],
        name="run_code",
        mounts=[instance.skills.bind(seed=seed), instance.store.bind(_Config(store="s"))],
    )
    assert published == []


def test_bind_returns_the_instances_mount():
    instance = Agent()
    skills = instance.skills.bind(seed=None)
    assert isinstance(skills, agent.VFSMount)
    assert (skills.path, skills.read_only, skills.description) == ("/skills", True, "Skills.")
    assert isinstance(skills.backend, agent.InMemoryFileSystem)

    store = instance.store.bind(_Config(store="s"))
    assert isinstance(store, IndexedVFSMount)
    assert store.index is instance.store.state
    assert (store.source.filesystem, store.source.config) == ("test-decl-store", {"store": "s"})


async def test_a_bound_in_memory_mount_keeps_its_files_in_the_state():
    instance = Agent()
    mount = instance.skills.bind(seed=None)
    await mount.backend.write(PurePosixPath("a.md"), b"hi")  # type: ignore[attr-defined]
    assert instance.skills.state.current.files["a.md"].content == "hi"


def test_each_instance_binds_its_own_mount_once():
    first, second = Agent(), Agent()
    assert first.store is first.store
    assert first.store is not second.store
    first.store.bind(_Config(store="s"))
    second.store.bind(_Config(store="t"))
    with pytest.raises(RuntimeError, match="already bound"):
        first.store.bind(_Config(store="s"))


def test_bind_takes_the_filesystems_own_config_and_an_explicit_seed():
    instance = Agent()
    with pytest.raises(TypeError, match="takes a _Config"):
        instance.store.bind(_Other())  # type: ignore[arg-type]
    instance.store.bind(_Config(store="s"))  # a rejected config does not use up the bind
    with pytest.raises(TypeError, match="seed"):
        instance.skills.bind()  # type: ignore[call-arg]


def test_an_unbound_mount_is_refused_with_a_hint():
    with pytest.raises(TypeError, match=r"self\.<name>\.bind"):
        agent.code_mode_tool([], name="run_code", mounts=[Agent().store])  # type: ignore[list-item]


def test_a_declared_mount_cannot_be_reassigned():
    with pytest.raises(AttributeError, match="cannot be reassigned"):
        Agent().store = None  # type: ignore[assignment]


def test_a_declaration_is_checked_when_it_is_made():
    with pytest.raises(ValueError, match="absolute, normalized"):
        agent.vfs_mount("skills", agent.InMemoryFileSystem, description="")
    with pytest.raises(TypeError, match="must be InMemoryFileSystem or an ActivityFileSystem"):
        agent.vfs_mount("/x", object, description="")  # type: ignore[call-overload]


# ---------------------------------------------------------------- OKF bundles


class OKFAgent:
    memory = agent.okf_bundle_vfs_mount("/memory", _Store, description="Memory.")


def test_an_okf_bundle_mount_records_the_okf_version_from_the_start():
    assert declared_states(OKFAgent)["memory"].state_type is agent.FileIndex
    index = OKFAgent().memory.state.current
    assert (index.mount, index.okf_version) == ("/memory", "0.2")
    assert Agent().store.state.current.okf_version is None


def test_an_okf_bundle_mount_is_marked_in_the_contract():
    tool = agent.code_mode_tool(
        [], name="run_code", mounts=[OKFAgent().memory.bind(_Config(store="s"))]
    )
    assert "- `/memory` (writable, OKF v0.2 bundle): Memory." in (tool.__doc__ or "")


def test_an_okf_bundle_mount_needs_an_activity_filesystem():
    with pytest.raises(TypeError, match="must be an ActivityFileSystem"):
        agent.okf_bundle_vfs_mount("/m", agent.InMemoryFileSystem, description="")  # type: ignore[arg-type]


def test_okf_code_mode_tool_takes_only_a_bound_okf_bundle():
    with pytest.raises(TypeError, match="okf_bundle_vfs_mount"):
        agent.okf_code_mode_tool(Agent().store.bind(_Config(store="s")), name="run_code")
    with pytest.raises(ValueError, match="'okf_bundle' is reserved"):
        agent.okf_code_mode_tool(
            OKFAgent().memory.bind(_Config(store="s")),
            name="run_code",
            injections={"okf_bundle": 1},
        )


def test_okf_code_mode_tool_keeps_extra_tools_and_mounts_and_explains_okf():
    @agent.tool_defn()
    async def lookup(q: str) -> str:
        """Look something up."""
        ...

    instance = Agent()
    tool = agent.okf_code_mode_tool(
        OKFAgent().memory.bind(_Config(store="s")),
        name="run_code",
        tools=[lookup],
        mounts=[instance.skills.bind(seed=None)],
    )
    doc = tool.__doc__ or ""
    for name in ("lookup", "okf_concepts", "okf_links", "okf_render"):
        assert f"async def {name}(" in doc
    assert "async def okf_concepts(prefix: str = ...)" in doc
    assert "- `/skills` (read-only)" in doc and "- `/memory` (writable, OKF v0.2 bundle)" in doc
    assert "Knowledge bundle: `/memory` is an Open Knowledge Format (OKF v0.2) bundle" in doc
