"""The Code Mode virtual filesystem: mounts a script reads and writes with ``open()`` and ``pathlib``.

A :class:`Mount` puts a :class:`FileSystem` backend at a virtual path. Backend methods are async
and run in workflow code, so a backend can reach storage through an activity, a Nexus operation,
a child workflow, or hold its files in workflow memory like :class:`InMemoryFileSystem`. Nothing
here touches the worker's disk: workflow tasks may land on any worker, and whatever a script
reads during a step is read again on replay.

Every file operation a script makes ends its step (see :mod:`.monty_stepper`). The driver then
maps Monty's operation onto backend calls (:func:`perform`), each dispatched through
``runner.run_tool`` as one of the ``fs_*`` tools below, so file access gets the agent's approval
policy and tool lifecycle events like any host call.

Sandbox-safe: this module never imports ``pydantic_monty``.
"""

from __future__ import annotations

import base64
import errno
import os
import posixpath
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.agent_workflow import Injected, tool_defn


@dataclass(frozen=True)
class FileStat:
    """What a backend reports about one path. ``mtime`` is a Unix timestamp."""

    is_dir: bool
    size: int = 0
    mtime: float = 0.0


class FileSystem(Protocol):
    """Storage behind a :class:`Mount`.

    Methods are awaited in workflow code, so their bodies must be workflow-safe. Paths are
    relative to the mount and normalized; the mount's root is ``PurePosixPath(".")``. Raise the
    built-in ``OSError`` subclasses (``FileNotFoundError``, ``IsADirectoryError``, ...) for
    failures; from an activity, raise ``ApplicationError`` with ``type`` set to the built-in
    name, and the script sees the built-in exception.

    ``write``, ``mkdir``, ``delete`` and ``rename`` are only needed for a writable mount.
    """

    async def stat(self, path: PurePosixPath) -> FileStat | None:
        """The path's metadata, or ``None`` when it does not exist."""
        ...

    async def read(self, path: PurePosixPath) -> bytes:
        """The file's whole content."""
        ...

    async def list(self, path: PurePosixPath) -> list[str]:
        """The names of the directory's entries."""
        ...


_WRITE_METHODS = ("write", "mkdir", "delete", "rename")


@dataclass(frozen=True)
class Mount:
    """A :class:`FileSystem` at a virtual path inside the sandbox.

    ``path`` is absolute and normalized (``/skills``, not ``/skills/`` or ``/a/../skills``).
    ``read_only`` is enforced before any backend call. ``description`` is shown to the model
    next to the mount, so say what is there and how to use it."""

    path: str
    backend: FileSystem
    read_only: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if not self.path.startswith("/") or posixpath.normpath(self.path) != self.path:
            raise ValueError(
                f"mount path {self.path!r} must be an absolute, normalized POSIX path, "
                f"like '/workspace'"
            )
        if self.path == "/":
            raise ValueError("a mount cannot be the root '/'; mount at a named directory")
        for method in ("stat", "read", "list"):
            if not callable(getattr(self.backend, method, None)):
                raise TypeError(
                    f"mount {self.path!r}: the backend has no {method}() method, so it is not "
                    f"a FileSystem"
                )
        if not self.read_only:
            missing = [m for m in _WRITE_METHODS if not callable(getattr(self.backend, m, None))]
            if missing:
                raise TypeError(
                    f"mount {self.path!r} is writable, but its backend has no "
                    f"{', '.join(missing)}() method. Implement them, or mount it read_only=True."
                )


def validate_mounts(mounts: Sequence[Mount]) -> tuple[Mount, ...]:
    """Reject mounts that repeat or nest, so every path belongs to at most one mount."""
    ordered = sorted(mounts, key=lambda m: m.path)
    for outer, inner in zip(ordered, ordered[1:]):
        if inner.path == outer.path or inner.path.startswith(outer.path + "/"):
            raise ValueError(f"mounts {outer.path!r} and {inner.path!r} overlap")
    return tuple(mounts)


def route(mounts: Sequence[Mount], path: PurePosixPath) -> tuple[Mount, PurePosixPath] | None:
    """The mount ``path`` falls in and the path relative to it, matching whole segments.

    Monty hands over absolute paths with ``.`` and ``..`` already collapsed, so no path that
    reaches this can step outside a mount by traversal."""
    text = str(path)
    for mount in mounts:
        if text == mount.path:
            return mount, PurePosixPath(".")
        if text.startswith(mount.path + "/"):
            return mount, PurePosixPath(text[len(mount.path) + 1 :])
    return None


# ---------------------------------------------------------------- the operation a step stopped at

# Monty's file operations. Every other OS call (clock, entropy, environment) is not a file one.
FILE_OPERATIONS = frozenset(
    {
        "open",
        "Path.exists",
        "Path.is_file",
        "Path.is_dir",
        "Path.is_symlink",
        "Path.read_text",
        "Path.read_bytes",
        "Path.write_text",
        "Path.write_bytes",
        "Path.append_text",
        "Path.append_bytes",
        "Path.mkdir",
        "Path.unlink",
        "Path.rmdir",
        "Path.iterdir",
        "Path.stat",
        "Path.rename",
        "Path.resolve",
        "Path.absolute",
    }
)

# Operations that change a mount. ``open`` changes it unless opened for reading only.
_WRITE_OPERATIONS = frozenset(
    {
        "Path.write_text",
        "Path.write_bytes",
        "Path.append_text",
        "Path.append_bytes",
        "Path.mkdir",
        "Path.unlink",
        "Path.rmdir",
        "Path.rename",
    }
)


def is_write(operation: str, args: Sequence[Any]) -> bool:
    if operation == "open":
        mode = str(args[1]) if len(args) > 1 else "r"
        return mode[0] != "r" or "+" in mode
    return operation in _WRITE_OPERATIONS


@dataclass(frozen=True)
class FileOp:
    """One file operation a script is blocked on, routed to its mount.

    ``path`` and ``target`` (a rename's destination, in the same mount) are mount-relative;
    ``virtual`` is the script's absolute path, used in the errors it sees. ``args`` are the
    operation's remaining arguments: the data of a write, the mode of an ``open``."""

    call_id: int
    operation: str
    mount: Mount
    path: PurePosixPath
    virtual: PurePosixPath
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    target: PurePosixPath | None = None


@dataclass(frozen=True)
class Opened:
    """An ``open`` that succeeded; the stepper answers it with a Monty file handle."""

    path: str
    mode: str


# ---------------------------------------------------------------- the fs_* tools

# The injected parameter every fs_* tool takes: the mount's backend, which the driver supplies
# and the tool's lifecycle events leave out.
BACKEND_INJECTION = "backend"


@tool_defn()
async def fs_seed(backend: Injected[Any], mount: str) -> str:
    """Load an in-memory filesystem's initial files, before the first operation on its mount."""
    count, size = await backend._seed_now()
    return f"seeded {count} files ({size} bytes)"


@tool_defn()
async def fs_stat(backend: Injected[Any], mount: str, path: str) -> FileStat | None:
    """Look up a path's metadata in a Code Mode filesystem mount."""
    return await backend.stat(PurePosixPath(path))


@tool_defn()
async def fs_read(backend: Injected[Any], mount: str, path: str) -> bytes:
    """Read a file from a Code Mode filesystem mount."""
    return await backend.read(PurePosixPath(path))


@tool_defn()
async def fs_list(backend: Injected[Any], mount: str, path: str) -> list[str]:
    """List a directory in a Code Mode filesystem mount."""
    return await backend.list(PurePosixPath(path))


@tool_defn()
async def fs_write(
    backend: Injected[Any],
    mount: str,
    path: str,
    content: str,
    encoding: Literal["utf-8", "base64"] = "utf-8",
) -> int:
    """Write a file in a Code Mode filesystem mount, replacing any existing content. Binary
    content arrives base64-encoded."""
    data = base64.b64decode(content) if encoding == "base64" else content.encode()
    await backend.write(PurePosixPath(path), data)
    return len(data)


@tool_defn()
async def fs_mkdir(backend: Injected[Any], mount: str, path: str) -> None:
    """Create a directory in a Code Mode filesystem mount."""
    await backend.mkdir(PurePosixPath(path))


@tool_defn()
async def fs_delete(backend: Injected[Any], mount: str, path: str) -> None:
    """Delete a file or an empty directory in a Code Mode filesystem mount."""
    await backend.delete(PurePosixPath(path))


@tool_defn()
async def fs_rename(backend: Injected[Any], mount: str, path: str, target: str) -> None:
    """Move a file or directory to another path in the same Code Mode filesystem mount."""
    await backend.rename(PurePosixPath(path), PurePosixPath(target))


# Runs one fs_* tool against a mount's backend: (tool, keyword arguments) -> its result.
CallTool = Callable[..., Awaitable[Any]]


class ToolCalls:
    """A mount's backend as a :class:`FileSystem` whose every call is an ``fs_*`` tool call made
    through ``call_tool``, which supplies the backend as the tools' injected argument."""

    def __init__(self, mount: Mount, call_tool: CallTool) -> None:
        self._mount = mount.path
        self._call_tool = call_tool

    async def stat(self, path: PurePosixPath) -> FileStat | None:
        return await self._call_tool(fs_stat, mount=self._mount, path=str(path))

    async def read(self, path: PurePosixPath) -> bytes:
        return await self._call_tool(fs_read, mount=self._mount, path=str(path))

    async def list(self, path: PurePosixPath) -> list[str]:
        return await self._call_tool(fs_list, mount=self._mount, path=str(path))

    async def write(self, path: PurePosixPath, data: bytes) -> None:
        try:
            content, encoding = data.decode(), "utf-8"
        except UnicodeDecodeError:
            content, encoding = base64.b64encode(data).decode(), "base64"
        await self._call_tool(
            fs_write, mount=self._mount, path=str(path), content=content, encoding=encoding
        )

    async def mkdir(self, path: PurePosixPath) -> None:
        await self._call_tool(fs_mkdir, mount=self._mount, path=str(path))

    async def delete(self, path: PurePosixPath) -> None:
        await self._call_tool(fs_delete, mount=self._mount, path=str(path))

    async def rename(self, path: PurePosixPath, target: PurePosixPath) -> None:
        await self._call_tool(fs_rename, mount=self._mount, path=str(path), target=str(target))


def _error(kind: type[OSError], code: int, path: PurePosixPath) -> OSError:
    return kind(code, os.strerror(code), str(path))


def _encode(data: str | bytes) -> bytes:
    return data.encode() if isinstance(data, str) else data


async def perform(op: FileOp, fs: Any) -> Any:
    """Carry out ``op`` as calls on ``fs`` (the mount's backend as a :class:`FileSystem`; the
    driver passes :class:`ToolCalls`), returning what the script's operation returns (an
    :class:`Opened` or :class:`FileStat` for the stepper to convert) or raising the exception
    the script sees. A backend's ``OSError`` that names a mount-relative path is raised naming
    the virtual path instead."""
    try:
        return await _perform(op, fs)
    except OSError as e:
        if e.errno is None or not e.filename or str(e.filename).startswith("/"):
            raise
        virtual = PurePosixPath(op.mount.path) / str(e.filename)
        raise type(e)(e.errno, e.strerror, str(virtual)) from e


async def _perform(op: FileOp, fs: Any) -> Any:
    path, virtual, name = op.path, op.virtual, op.operation

    async def existing() -> FileStat:
        found = await fs.stat(path)
        if found is None:
            raise _error(FileNotFoundError, errno.ENOENT, virtual)
        return found

    if name == "Path.exists":
        return await fs.stat(path) is not None
    if name in ("Path.is_file", "Path.is_dir"):
        found = await fs.stat(path)
        return found is not None and found.is_dir == (name == "Path.is_dir")
    if name == "Path.stat":
        return await existing()
    if name == "open":
        mode = str(op.args[0]) if op.args else "r"
        found = await fs.stat(path)
        if found is not None and found.is_dir:
            raise _error(IsADirectoryError, errno.EISDIR, virtual)
        if mode[0] == "r" and found is None:
            raise _error(FileNotFoundError, errno.ENOENT, virtual)
        if mode[0] == "w" or (mode[0] == "a" and found is None):
            await fs.write(path, b"")
        return Opened(path=str(virtual), mode=mode)
    if name in ("Path.read_text", "Path.read_bytes"):
        if (await existing()).is_dir:
            raise _error(IsADirectoryError, errno.EISDIR, virtual)
        data = await fs.read(path)
        return data.decode() if name == "Path.read_text" else data
    if name in ("Path.write_text", "Path.write_bytes"):
        data = _encode(op.args[0])
        await fs.write(path, data)
        return len(op.args[0])
    if name in ("Path.append_text", "Path.append_bytes"):
        found = await fs.stat(path)
        if found is not None and found.is_dir:
            raise _error(IsADirectoryError, errno.EISDIR, virtual)
        before = await fs.read(path) if found is not None else b""
        await fs.write(path, before + _encode(op.args[0]))
        return len(op.args[0])
    if name == "Path.iterdir":
        if not (await existing()).is_dir:
            raise _error(NotADirectoryError, errno.ENOTDIR, virtual)
        return [virtual / entry for entry in sorted(await fs.list(path))]
    if name == "Path.mkdir":
        return await _mkdir(fs, op)
    if name == "Path.unlink":
        if (await existing()).is_dir:
            raise _error(IsADirectoryError, errno.EISDIR, virtual)
        await fs.delete(path)
        return None
    if name == "Path.rmdir":
        if path == PurePosixPath("."):
            raise _error(PermissionError, errno.EACCES, virtual)
        if not (await existing()).is_dir:
            raise _error(NotADirectoryError, errno.ENOTDIR, virtual)
        if await fs.list(path):
            raise _error(OSError, errno.ENOTEMPTY, virtual)
        await fs.delete(path)
        return None
    if name == "Path.rename":
        assert op.target is not None
        await existing()
        await fs.rename(path, op.target)
        return None
    raise NotImplementedError(name)


async def _mkdir(fs: Any, op: FileOp) -> None:
    parents = bool(op.kwargs.get("parents", False))
    exist_ok = bool(op.kwargs.get("exist_ok", False))
    found = await fs.stat(op.path)
    if found is not None:
        if exist_ok and found.is_dir:
            return
        raise _error(FileExistsError, errno.EEXIST, op.virtual)
    missing: list[PurePosixPath] = []
    for ancestor in reversed(op.path.parents[:-1]):  # outermost first, the mount root left out
        above = await fs.stat(ancestor)
        if above is None:
            if not parents:
                raise _error(FileNotFoundError, errno.ENOENT, op.virtual)
            missing.append(ancestor)
        elif not above.is_dir:
            raise _error(NotADirectoryError, errno.ENOTDIR, op.virtual)
    for directory in [*missing, op.path]:
        await fs.mkdir(directory)


# ---------------------------------------------------------------- the in-memory backend

Seed = Callable[[], Awaitable[Mapping[str, str | bytes]]]


class InMemoryFileSystem:
    """A :class:`FileSystem` held in workflow memory.

    Build it in ``@agent.init``, so each workflow gets its own; its files then persist across
    the conversation's scripts, and replay rebuilds them. ``seed`` is an async function, run in
    workflow code, returning the initial files as ``{mount-relative path: content}``; it runs
    once, before the first operation on the mount. Load files from disk inside an activity the
    seed awaits: the activity's result is recorded, so a running workflow keeps the files it
    seeded even after a deploy changes them. ``max_bytes`` caps the total size of the files,
    seeded ones included."""

    def __init__(self, *, seed: Seed | None = None, max_bytes: int | None = None) -> None:
        self._seed = seed
        self._seeded = seed is None
        self._max_bytes = max_bytes
        self._files: dict[PurePosixPath, bytes] = {}
        self._dirs: set[PurePosixPath] = {PurePosixPath(".")}

    @property
    def needs_seed(self) -> bool:
        return not self._seeded

    async def _seed_now(self) -> tuple[int, int]:
        """Run the seed and load its files. Called by the ``fs_seed`` tool."""
        assert self._seed is not None
        files = await self._seed()
        self._seeded = True
        for raw, content in files.items():
            path = PurePosixPath(posixpath.normpath(raw))
            if path.is_absolute() or path.parts[:1] == ("..",):
                raise ValueError(f"seeded path {raw!r} must be relative to the mount")
            for ancestor in path.parents:
                self._dirs.add(ancestor)
            await self.write(path, _encode(content))
        return len(files), sum(len(_encode(c)) for c in files.values())

    async def stat(self, path: PurePosixPath) -> FileStat | None:
        if path in self._dirs:
            return FileStat(is_dir=True)
        if path in self._files:
            return FileStat(is_dir=False, size=len(self._files[path]))
        return None

    async def read(self, path: PurePosixPath) -> bytes:
        if path in self._dirs:
            raise _error(IsADirectoryError, errno.EISDIR, path)
        try:
            return self._files[path]
        except KeyError:
            raise _error(FileNotFoundError, errno.ENOENT, path) from None

    async def list(self, path: PurePosixPath) -> list[str]:
        if path not in self._dirs:
            raise _error(NotADirectoryError, errno.ENOTDIR, path)
        entries = [p for p in (*self._dirs, *self._files) if p != path and p.parent == path]
        return sorted(p.name for p in entries)

    async def write(self, path: PurePosixPath, data: bytes) -> None:
        if path in self._dirs:
            raise _error(IsADirectoryError, errno.EISDIR, path)
        if path.parent not in self._dirs:
            raise _error(FileNotFoundError, errno.ENOENT, path)
        if self._max_bytes is not None:
            total = sum(len(d) for d in self._files.values()) - len(self._files.get(path, b""))
            if total + len(data) > self._max_bytes:
                raise OSError(
                    errno.ENOSPC,
                    f"No space left on device: the filesystem is limited to "
                    f"{self._max_bytes} bytes",
                    str(path),
                )
        self._files[path] = bytes(data)

    async def mkdir(self, path: PurePosixPath) -> None:
        if path in self._dirs or path in self._files:
            raise _error(FileExistsError, errno.EEXIST, path)
        if path.parent not in self._dirs:
            raise _error(FileNotFoundError, errno.ENOENT, path)
        self._dirs.add(path)

    async def delete(self, path: PurePosixPath) -> None:
        if path in self._files:
            del self._files[path]
        elif path in self._dirs and path != PurePosixPath("."):
            if await self.list(path):
                raise _error(OSError, errno.ENOTEMPTY, path)
            self._dirs.remove(path)
        else:
            raise _error(FileNotFoundError, errno.ENOENT, path)

    async def rename(self, path: PurePosixPath, target: PurePosixPath) -> None:
        if target.parent not in self._dirs:
            raise _error(FileNotFoundError, errno.ENOENT, target)
        if path in self._files:
            if target in self._dirs:
                raise _error(IsADirectoryError, errno.EISDIR, target)
            self._files[target] = self._files.pop(path)
            return
        if path not in self._dirs or path == PurePosixPath("."):
            raise _error(FileNotFoundError, errno.ENOENT, path)
        if target == path or path in target.parents:
            raise _error(OSError, errno.EINVAL, target)
        if target in self._files or (target in self._dirs and await self.list(target)):
            raise _error(OSError, errno.ENOTEMPTY, target)

        def moved(p: PurePosixPath) -> PurePosixPath:
            return target / p.relative_to(path) if p == path or path in p.parents else p

        self._dirs = {moved(p) for p in self._dirs}
        self._files = {moved(p): d for p, d in self._files.items()}
