# ABOUTME: Unit tests for the Code Mode virtual filesystem — mount validation and routing, the
# stepper stopping at file operations, the mapping of Monty's operations onto a FileSystem, and
# InMemoryFileSystem. Scripts run in the real sandbox; each file operation a step stops at is
# carried out directly against the mount's backend, standing in for the driver's fs_* tools.

from __future__ import annotations

import errno
from pathlib import PurePosixPath
from typing import Any

import pytest

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.code_mode import monty_stepper
from temporal_agent_harness.harness.code_mode.vfs import (
    FileEntry,
    FileIndex,
    FileOp,
    FileTree,
    InMemoryFileSystem,
    FileSource,
    IndexEntry,
    IndexedVFSMount,
    ToolCalls,
    VFSMount,
    perform,
    route,
    validate_mounts,
)
from temporal_agent_harness.harness.state import StatePatch, StateRef


@agent.tool_defn()
async def beta(x: int) -> int:
    """Do the beta thing."""
    ...


def _no_os(name: str, args: tuple) -> object:
    raise NotImplementedError(name)


def _stubs(mounts: list[VFSMount]):
    return agent.code_mode_tool([beta], name="run_code", mounts=mounts).__code_mode_stubs__


async def _carry_out(op: FileOp) -> Any:
    """What the driver does for a file operation, minus the fs_* tool dispatch."""
    backend = op.mount.backend
    try:
        if isinstance(backend, InMemoryFileSystem) and backend.needs_seed:
            await backend._seed_now()
        return await perform(op, backend)
    except Exception as e:
        return e


async def _run(script: str, mounts: list[VFSMount], host: dict[str, Any] | None = None):
    """Run ``script`` to completion and return its last step plus every file operation it
    stopped at."""
    run = monty_stepper.ScriptRun(script, _stubs(mounts), answer_os=_no_os, mounts=mounts)
    step = run.start()
    stopped_at: list[FileOp] = []
    while not step.done:
        results = {}
        for item in step.awaiting:
            if isinstance(item, FileOp):
                stopped_at.append(item)
                results[item.call_id] = await _carry_out(item)
            else:
                results[item.call_id] = (host or {})[item.function_name](*item.args)
        step = run.resume(results)
    assert step.error is None, step.error
    return step, stopped_at


def _skills() -> InMemoryFileSystem:
    async def seed() -> dict[str, str | bytes]:
        return {
            "pdf/SKILL.md": "---\nname: pdf\n---\nRead PDFs.\n",
            "pdf/reference/forms.md": "Forms.\n",
            "logo.png": b"\x89PNG\x00\xff",
        }

    return InMemoryFileSystem(seed=seed)


# ---------------------------------------------------------------- reading


async def test_a_script_reads_seeded_files_with_open_and_pathlib():
    mounts = [VFSMount("/skills", _skills(), read_only=True)]
    script = (
        "from pathlib import Path\n"
        "with open('/skills/pdf/SKILL.md') as f:\n"
        "    head = f.readline()\n"
        "    rest = f.read()\n"
        "[\n"
        "    head,\n"
        "    rest.splitlines()[-1],\n"
        "    sorted(p.name for p in Path('/skills').iterdir()),\n"
        "    Path('/skills/pdf/reference/forms.md').read_text(),\n"
        "    Path('/skills/logo.png').read_bytes(),\n"
        "    Path('/skills/pdf/SKILL.md').stat().st_size,\n"
        "    Path('/skills/pdf').is_dir(),\n"
        "    Path('/skills/missing.md').exists(),\n"
        "]\n"
    )
    step, stopped_at = await _run(script, mounts)
    assert step.output == [
        "---\n",
        "Read PDFs.",
        ["logo.png", "pdf"],
        "Forms.\n",
        b"\x89PNG\x00\xff",
        len("---\nname: pdf\n---\nRead PDFs.\n"),
        True,
        False,
    ]
    # Every file operation ended a step, and reached the backend mount-relative.
    assert stopped_at[0].operation == "open"
    assert stopped_at[0].path == PurePosixPath("pdf/SKILL.md")
    assert stopped_at[0].virtual == PurePosixPath("/skills/pdf/SKILL.md")


async def test_a_missing_file_raises_the_builtin_error_with_the_virtual_path():
    mounts = [VFSMount("/skills", _skills(), read_only=True)]
    script = (
        "try:\n"
        "    open('/skills/nope.md').read()\n"
        "    out = 'read'\n"
        "except FileNotFoundError as e:\n"
        "    out = str(e)\n"
        "out\n"
    )
    step, _ = await _run(script, mounts)
    assert step.output == "[Errno 2] No such file or directory: '/skills/nope.md'"


async def test_stat_times_never_come_from_the_worker_clock():
    mounts = [VFSMount("/skills", _skills(), read_only=True)]
    step, _ = await _run(
        "from pathlib import Path\nPath('/skills/pdf/SKILL.md').stat().st_mtime\n", mounts
    )
    assert step.output == 0.0


# ---------------------------------------------------------------- writing


async def test_writes_persist_across_scripts_on_the_same_filesystem():
    workspace = InMemoryFileSystem()
    mounts = [VFSMount("/workspace", workspace)]
    write = (
        "from pathlib import Path\n"
        "Path('/workspace/out').mkdir()\n"
        "with open('/workspace/out/a.txt', 'w') as f:\n"
        "    f.write('ab')\n"
        "    f.write('cd')\n"
        "with open('/workspace/out/a.txt', 'a') as f:\n"
        "    f.write('ef')\n"
        "Path('/workspace/out/b.bin').write_bytes(b'\\x00\\xff')\n"
    )
    await _run(write, mounts)
    read = (
        "from pathlib import Path\n"
        "[Path('/workspace/out/a.txt').read_text(), Path('/workspace/out/b.bin').read_bytes()]\n"
    )
    step, _ = await _run(read, mounts)
    assert step.output == ["abcdef", b"\x00\xff"]


async def test_directory_operations():
    mounts = [VFSMount("/workspace", InMemoryFileSystem())]
    script = (
        "from pathlib import Path\n"
        "out = []\n"
        "Path('/workspace/a/b/c').mkdir(parents=True)\n"
        "Path('/workspace/a/b/c').mkdir(parents=True, exist_ok=True)\n"
        "Path('/workspace/a/b/c/f.txt').write_text('x')\n"
        "for attempt in ('/workspace/a/b/c', '/workspace/x/y'):\n"
        "    try:\n"
        "        if attempt.endswith('c'):\n"
        "            Path(attempt).rmdir()\n"
        "        else:\n"
        "            Path(attempt).mkdir()\n"
        "    except OSError as e:\n"
        "        out.append(type(e).__name__)\n"
        "Path('/workspace/a/b').rename('/workspace/moved')\n"
        "out.append(sorted(p.name for p in Path('/workspace/moved/c').iterdir()))\n"
        "Path('/workspace/moved/c/f.txt').unlink()\n"
        "Path('/workspace/moved/c').rmdir()\n"
        "out.append(Path('/workspace/moved/c').exists())\n"
        "out.append(Path('/workspace/a').is_dir())\n"
        "out\n"
    )
    step, _ = await _run(script, mounts)
    assert step.output == ["OSError", "FileNotFoundError", ["f.txt"], False, True]


async def test_max_bytes_caps_what_an_in_memory_filesystem_holds():
    mounts = [VFSMount("/workspace", InMemoryFileSystem(max_bytes=4))]
    script = (
        "from pathlib import Path\n"
        "Path('/workspace/a').write_text('abcd')\n"
        "try:\n"
        "    Path('/workspace/b').write_text('e')\n"
        "    out = 'written'\n"
        "except OSError as e:\n"
        "    out = str(e)\n"
        "out\n"
    )
    step, _ = await _run(script, mounts)
    assert step.output.startswith(f"[Errno {errno.ENOSPC}] No space left on device")
    # The backend named the path relative to its mount; the script sees the virtual path.
    assert step.output.endswith("'/workspace/b'")


async def test_a_read_only_mount_refuses_writes_without_reaching_the_backend():
    mounts = [VFSMount("/skills", _skills(), read_only=True)]
    script = (
        "from pathlib import Path\n"
        "out = []\n"
        "for write in (lambda: Path('/skills/x').write_text('x'), lambda: open('/skills/y', 'w')):\n"
        "    try:\n"
        "        write()\n"
        "    except PermissionError:\n"
        "        out.append('denied')\n"
        "out\n"
    )
    step, stopped_at = await _run(script, mounts)
    assert step.output == ["denied", "denied"]
    assert stopped_at == []


async def test_a_rename_across_mounts_is_refused():
    mounts = [VFSMount("/a", InMemoryFileSystem()), VFSMount("/b", InMemoryFileSystem())]
    script = (
        "from pathlib import Path\n"
        "Path('/a/f').write_text('x')\n"
        "try:\n"
        "    Path('/a/f').rename('/b/f')\n"
        "    out = 'moved'\n"
        "except OSError as e:\n"
        "    out = str(e)\n"
        "out\n"
    )
    step, _ = await _run(script, mounts)
    assert step.output == f"[Errno {errno.EXDEV}] Invalid cross-device link: '/b/f'"


# ---------------------------------------------------------------- routing


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "/workspace-evil/x", "/workspace/../etc/passwd", "relative.txt"]
)
async def test_paths_outside_every_mount_stay_unhandled(path):
    mounts = [VFSMount("/workspace", InMemoryFileSystem())]
    run = monty_stepper.ScriptRun(
        f"open({path!r}).read()\n", _stubs(mounts), answer_os=_no_os, mounts=mounts
    )
    step = run.start()
    assert step.done and step.error and not step.aborted


def test_route_matches_whole_segments():
    workspace = VFSMount("/workspace", InMemoryFileSystem())
    assert route([workspace], PurePosixPath("/workspace")) == (workspace, PurePosixPath("."))
    assert route([workspace], PurePosixPath("/workspace/a/b")) == (
        workspace,
        PurePosixPath("a/b"),
    )
    assert route([workspace], PurePosixPath("/workspaces/a")) is None


# ---------------------------------------------------------------- concurrency


async def test_calls_made_before_a_file_operation_are_handed_over_to_start():
    mounts = [VFSMount("/workspace", InMemoryFileSystem())]
    script = (
        "import asyncio\n"
        "from pathlib import Path\n"
        "async def main():\n"
        "    pending = beta(1)\n"
        "    exists = Path('/workspace/f').exists()\n"
        "    return [exists, await pending]\n"
        "asyncio.run(main())\n"
    )
    run = monty_stepper.ScriptRun(script, _stubs(mounts), answer_os=_no_os, mounts=mounts)
    step = run.start()
    [op] = step.awaiting
    assert isinstance(op, FileOp) and op.operation == "Path.exists"
    assert [(c.function_name, c.args) for c in step.started] == [("beta", [1])]
    call = step.started[0]

    step = run.resume({op.call_id: False})
    # The call is awaited now; it was already handed over, so nothing new starts.
    assert step.awaiting == [call] and step.started == []
    step = run.resume({call.call_id: 10})
    assert step.output == [False, 10]


# ---------------------------------------------------------------- construction


def test_mount_paths_must_be_absolute_and_normalized():
    for bad in ("workspace", "/workspace/", "/a/../workspace", "/"):
        with pytest.raises(ValueError):
            VFSMount(bad, InMemoryFileSystem())


def test_overlapping_mounts_are_rejected():
    with pytest.raises(ValueError, match="overlap"):
        validate_mounts([VFSMount("/a", InMemoryFileSystem()), VFSMount("/a/b", InMemoryFileSystem())])
    with pytest.raises(ValueError, match="overlap"):
        validate_mounts([VFSMount("/a", InMemoryFileSystem()), VFSMount("/a", InMemoryFileSystem())])
    validate_mounts([VFSMount("/a", InMemoryFileSystem()), VFSMount("/ab", InMemoryFileSystem())])


def test_a_writable_mount_needs_a_writable_backend():
    class ReadOnly:
        async def stat(self, path): ...
        async def read(self, path): ...
        async def list(self, path): ...

    with pytest.raises(TypeError, match="read_only=True"):
        VFSMount("/data", ReadOnly())
    VFSMount("/data", ReadOnly(), read_only=True)
    with pytest.raises(TypeError, match="not a FileSystem"):
        VFSMount("/data", object(), read_only=True)


async def test_seeded_paths_must_be_relative():
    async def seed() -> dict[str, str | bytes]:
        return {"/etc/passwd": "x"}

    with pytest.raises(ValueError, match="inside the mount"):
        await InMemoryFileSystem(seed=seed)._seed_now()


def test_the_contract_lists_the_mounts_and_the_stubs_declare_open():
    tool = agent.code_mode_tool(
        [beta],
        name="run_code",
        mounts=[
            VFSMount("/skills", _skills(), read_only=True, description="Agent skills."),
            VFSMount("/workspace", InMemoryFileSystem()),
        ],
    )
    doc = tool.__doc__ or ""
    assert "- `/skills` (read-only): Agent skills." in doc
    assert "- `/workspace` (writable)" in doc
    assert "no filesystem" not in doc
    assert "def open(" in tool.__code_mode_stubs__.source

    plain = agent.code_mode_tool([beta], name="run_code")
    assert "no filesystem" in (plain.__doc__ or "")
    assert "def open(" not in plain.__code_mode_stubs__.source


def test_a_tool_with_mounts_needs_no_host_functions():
    tool = agent.code_mode_tool(
        [], name="run_code", mounts=[VFSMount("/workspace", InMemoryFileSystem())]
    )
    assert "Host functions available:\n\n(none)" in (tool.__doc__ or "")


def test_open_type_checks_only_with_mounts():
    script = "with open('/workspace/a.txt', 'w') as f:\n    f.write('x')\n"
    with_mounts = agent.code_mode_tool(
        [beta], name="run_code", mounts=[VFSMount("/workspace", InMemoryFileSystem())]
    )
    assert monty_stepper.type_check(script, with_mounts.__code_mode_stubs__) is None
    plain = agent.code_mode_tool([beta], name="run_code")
    assert monty_stepper.type_check(script, plain.__code_mode_stubs__) is not None


# ---------------------------------------------------------------- state tracking


def _tracked(
    initial: FileTree | None = None, *, mount: str = "/workspace", read_only: bool = False
) -> tuple[StateRef[FileTree], list[StatePatch]]:
    """A tracked tree whose header already names its mount, so the only patches are the
    script's changes."""
    tree = (initial or FileTree()).model_copy(update={"mount": mount, "read_only": read_only})
    patches: list[StatePatch] = []
    return StateRef("workspace", tree, publish=patches.append), patches


def _in_state(
    ref: StateRef[FileTree], *, seed: Any = None, max_bytes: int | None = None
) -> InMemoryFileSystem:
    return InMemoryFileSystem._in_state(ref, seed=seed, max_bytes=max_bytes)


async def test_a_tracked_filesystem_publishes_one_patch_per_change():
    ref, patches = _tracked()
    mounts = [VFSMount("/workspace", _in_state(ref))]
    script = (
        "from pathlib import Path\n"
        "Path('/workspace/notes').mkdir()\n"
        "Path('/workspace/notes/a.txt').write_text('hi')\n"
        "Path('/workspace/logo.bin').write_bytes(b'\\x00\\xff')\n"
        "Path('/workspace/notes/a.txt').write_text('hello')\n"
        "Path('/workspace/logo.bin').unlink()\n"
    )
    await _run(script, mounts)

    assert [[op["op"] for op in p.ops] for p in patches] == [
        ["add"],  # mkdir
        ["add"],  # new file
        ["add"],  # new binary file
        ["replace"],  # overwrite
        ["remove"],  # unlink
    ]
    # Keys are mount-relative paths, escaped as JSON Pointer segments.
    assert patches[1].ops[0]["path"] == "/files/notes~1a.txt"
    assert patches[1].ops[0]["value"] == {"content": "hi", "encoding": "utf-8", "size": 2}
    assert patches[2].ops[0]["value"] == {"content": "AP8=", "encoding": "base64", "size": 2}
    assert ref.current.directories == ["notes"]
    assert list(ref.current.files) == ["notes/a.txt"]
    assert [p.version for p in patches] == [1, 2, 3, 4, 5]


async def test_a_tracked_seed_is_one_patch():
    async def seed() -> dict[str, str | bytes]:
        return {"a/b/one.md": "1", "a/two.md": "2"}

    ref, patches = _tracked(mount="/skills", read_only=True)
    mounts = [VFSMount("/skills", _in_state(ref, seed=seed), read_only=True)]
    step, _ = await _run(
        "from pathlib import Path\nPath('/skills/a/b/one.md').read_text()\n", mounts
    )
    assert step.output == "1"
    assert len(patches) == 1
    assert ref.current.directories == ["a", "a/b"]
    assert set(ref.current.files) == {"a/b/one.md", "a/two.md"}


async def test_a_tracked_rename_moves_a_directory_in_one_patch():
    ref, patches = _tracked(
        FileTree(
            directories=["src", "src/pkg"],
            files={"src/pkg/mod.py": FileEntry(content="x = 1", size=5)},
        )
    )
    mounts = [VFSMount("/workspace", _in_state(ref))]
    script = (
        "from pathlib import Path\n"
        "Path('/workspace/src').rename('/workspace/lib')\n"
        "Path('/workspace/lib/pkg/mod.py').read_text()\n"
    )
    step, _ = await _run(script, mounts)
    assert step.output == "x = 1"
    assert len(patches) == 1
    assert sorted(ref.current.directories) == ["lib", "lib/pkg"]
    assert list(ref.current.files) == ["lib/pkg/mod.py"]


async def test_the_initial_state_is_the_filesystems_initial_contents():
    ref, patches = _tracked(FileTree(files={"todo.md": FileEntry(content="- ship it", size=9)}))
    mounts = [VFSMount("/workspace", _in_state(ref))]
    step, _ = await _run(
        "from pathlib import Path\n[p.name for p in Path('/workspace').iterdir()]\n", mounts
    )
    assert step.output == ["todo.md"]
    assert patches == []


async def test_max_bytes_applies_to_a_tracked_filesystem():
    ref, patches = _tracked()
    mounts = [VFSMount("/workspace", _in_state(ref, max_bytes=3))]
    script = (
        "from pathlib import Path\n"
        "try:\n"
        "    Path('/workspace/a').write_text('abcd')\n"
        "    out = 'written'\n"
        "except OSError:\n"
        "    out = 'full'\n"
        "out\n"
    )
    step, _ = await _run(script, mounts)
    assert step.output == "full"
    assert patches == [] and ref.current.files == {}


# ---------------------------------------------------------------- file index


async def _run_indexed(script: str, mounts: list[VFSMount]):
    """Run ``script`` the way the driver does, through ``ToolCalls`` (so indexes are kept),
    with each fs_* tool call carried out directly on the mount's backend."""

    def tools_for(backend: Any):
        async def call_tool(tool: Any, **kw: Any) -> Any:
            path = PurePosixPath(kw.get("path", "."))
            name = tool.__name__
            if name == "fs_write":
                return await backend.write(path, kw["content"].encode())
            if name == "fs_rename":
                return await backend.rename(path, PurePosixPath(kw["target"]))
            return await getattr(backend, name.removeprefix("fs_"))(path)

        return call_tool

    async def carry_out(op: FileOp) -> Any:
        try:
            return await perform(op, ToolCalls(op.mount, tools_for(op.mount.backend)))
        except Exception as e:
            return e

    run = monty_stepper.ScriptRun(script, _stubs(mounts), answer_os=_no_os, mounts=mounts)
    step = run.start()
    while not step.done:
        step = run.resume({op.call_id: await carry_out(op) for op in step.awaiting})
    assert step.error is None, step.error
    return step


def _indexed(
    initial: dict[str, IndexEntry] | None = None,
) -> tuple[StateRef[FileIndex], list[StatePatch], IndexedVFSMount]:
    patches: list[StatePatch] = []
    ref = StateRef("files", FileIndex(mount="/w", entries=initial or {}), publish=patches.append)
    mount = IndexedVFSMount(
        "/w", InMemoryFileSystem(), index=ref, source=FileSource(filesystem="fake", config={})
    )
    return ref, patches, mount


async def test_an_index_records_what_scripts_do_and_see():
    ref, patches, mount = _indexed()
    script = (
        "from pathlib import Path\n"
        "Path('/w/notes').mkdir()\n"
        "Path('/w/notes/a.md').write_text('hi')\n"
        "Path('/w/notes/a.md').write_text('hello')\n"
        "Path('/w/old.md').write_text('x')\n"
        "Path('/w/notes').rename('/w/docs')\n"
        "Path('/w/old.md').unlink()\n"
        "Path('/w/docs/a.md').read_text()\n"
    )
    step = await _run_indexed(script, [mount])
    assert step.output == "hello"
    assert ref.current.entries == {
        "docs": IndexEntry(is_dir=True),
        "docs/a.md": IndexEntry(is_dir=False, size=5, writes=2),
    }
    # Metadata only: no patch ever carries a file's contents.
    assert "hello" not in repr([p.ops for p in patches])


async def test_a_listing_adds_what_it_names_and_drops_what_it_no_longer_does():
    ref, _, mount = _indexed(
        {"gone": IndexEntry(is_dir=True), "gone/x.md": IndexEntry(is_dir=False, size=1)}
    )
    await mount.backend.write(PurePosixPath("here.md"), b"1")
    script = "from pathlib import Path\n[p.name for p in Path('/w').iterdir()]\n"
    step = await _run_indexed(script, [mount])
    assert step.output == ["here.md"]
    assert ref.current.entries == {"here.md": IndexEntry()}


async def test_a_lookup_that_finds_nothing_drops_the_path():
    ref, _, mount = _indexed({"x.md": IndexEntry(is_dir=False, size=1)})
    step = await _run_indexed("from pathlib import Path\nPath('/w/x.md').exists()\n", [mount])
    assert step.output is False
    assert ref.current.entries == {}


async def test_an_operation_that_changes_nothing_publishes_nothing():
    ref, patches, mount = _indexed()
    await mount.backend.write(PurePosixPath("a.md"), b"abc")
    script = "from pathlib import Path\nPath('/w/a.md').read_text()\nPath('/w/a.md').read_text()\n"
    await _run_indexed(script, [mount])
    assert ref.current.entries == {"a.md": IndexEntry(is_dir=False, size=3)}
    assert len(patches) == 1
