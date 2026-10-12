"""``ActivityFileSystem``: a Code Mode filesystem of plain async methods that run as activities.

Subclass it with a pydantic config model and a name, and write the filesystem's operations as
ordinary async methods; they run on a worker, so they may touch disk, the network or a storage
service::

    class LocalDiskConfig(BaseModel):
        directory: PurePosixPath

    class LocalDisk(ActivityFileSystem[LocalDiskConfig], name="local-disk"):
        def __init__(self, config: LocalDiskConfig) -> None:
            super().__init__(config)
            self._root = ...  # validate the config here; it is untrusted input

        async def stat(self, path: PurePosixPath) -> FileStat | None: ...
        async def read(self, path: PurePosixPath, offset: int = 0,
                       length: int | None = None) -> bytes: ...
        async def list(self, path: PurePosixPath) -> list[str]: ...
        # and, for a writable mount: write, mkdir, delete, rename

The harness turns each method into an activity named ``vfs.<name>.<method>`` (see
:attr:`ActivityFileSystem.activities`; ``AgentHarnessPlugin(filesystems=...)`` registers them),
plus ``vfs.<name>.view``, the read-only chunked read the console's file viewer calls as a
standalone activity, and ``vfs.<name>.okf_graph``, which walks the filesystem as an OKF bundle
(see :mod:`.okf`). Every activity builds a fresh instance from the config it is given, so the
config is validated on every call. In the workflow, ``LocalDisk.backend(config)`` is the mount's
backend: each operation runs the matching activity on the workflow's task queue.

A config arrives from whoever calls the activity, the console's file viewer included, so treat it
as untrusted: accept only what is safe to act on, and refuse anything else in ``__init__``.
"""

from __future__ import annotations

import base64
import errno
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, ClassVar, Generic, Literal, TypeVar, get_args, get_origin

from pydantic import BaseModel, JsonValue
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from .okf import OKFGraph, build_okf_graph
from .vfs import FileSource, FileStat

# Covariant: a filesystem only ever reads its config, so a ``LocalDisk`` is an
# ``ActivityFileSystem[BaseModel]`` wherever any filesystem will do.
C = TypeVar("C", bound=BaseModel, covariant=True)
T = TypeVar("T")

READ_METHODS = ("stat", "read", "list")
WRITE_METHODS = ("write", "mkdir", "delete", "rename")

# The most one view call returns; the console pages through anything larger.
MAX_VIEW_BYTES = 256_000

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")

# Raised by a filesystem method, these reach the script as the built-in of the same name.
_OS_ERRORS = (
    FileNotFoundError,
    FileExistsError,
    IsADirectoryError,
    NotADirectoryError,
    PermissionError,
)


class FsCall(BaseModel):
    """One operation on a path, against the filesystem the config describes."""

    config: dict[str, JsonValue]
    path: PurePosixPath


class FsRead(FsCall):
    offset: int = 0
    length: int | None = None


class FsWrite(FsCall):
    """A whole file to write. Text is sent as is, anything else as base64."""

    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"


class FsRename(FsCall):
    target: PurePosixPath


class OKFGraphCall(FsCall):
    """Walk the OKF bundle under ``path`` (``.`` for the whole mount); ``links`` reads whole
    files to find the links between concepts, where without it only their frontmatter is read."""

    links: bool = True


class StatResult(BaseModel):
    """A path's metadata, or ``None`` when it does not exist."""

    found: FileStat | None


class FileContent(BaseModel):
    """File bytes on the wire. Text is sent as is, anything else as base64."""

    content: str
    encoding: Literal["utf-8", "base64"] = "utf-8"


class FileChunk(FileContent):
    """One page of a file for the console's viewer: ``content`` starts at ``offset``, and
    ``size`` and ``mtime`` describe the whole file when the page was read."""

    offset: int
    size: int
    mtime: float


def _wire(data: bytes) -> tuple[str, Literal["utf-8", "base64"]]:
    try:
        return data.decode(), "utf-8"
    except UnicodeDecodeError:
        return base64.b64encode(data).decode(), "base64"


def _unwire(content: str, encoding: Literal["utf-8", "base64"]) -> bytes:
    return base64.b64decode(content) if encoding == "base64" else content.encode()


def activity_name(filesystem: str, operation: str) -> str:
    """The registered name of a filesystem's activity, e.g. ``vfs.local-disk.read``."""
    return f"vfs.{filesystem}.{operation}"


class ActivityFileSystem(Generic[C]):
    """Base class for a filesystem whose operations run as activities. See the module docs.

    Subclasses must override ``stat``, ``read`` and ``list``; override ``write``, ``mkdir``,
    ``delete`` and ``rename`` too to back a writable mount. Raise the built-in ``OSError``
    subclasses (``FileNotFoundError``, ...) for failures; the script sees the same built-in.
    """

    name: ClassVar[str]
    config_type: ClassVar[type[BaseModel]]
    activities: ClassVar[list[Callable[..., Awaitable[object]]]]
    writable: ClassVar[bool]

    def __init__(self, config: C) -> None:
        self._config = config

    @property
    def config(self) -> C:
        return self._config

    async def stat(self, path: PurePosixPath) -> FileStat | None:
        """The path's metadata, or ``None`` when it does not exist."""
        raise NotImplementedError

    async def read(
        self, path: PurePosixPath, offset: int = 0, length: int | None = None
    ) -> bytes:
        """Up to ``length`` bytes of the file from ``offset`` (all of it when ``None``)."""
        raise NotImplementedError

    async def list(self, path: PurePosixPath) -> list[str]:
        """The names of the directory's entries."""
        raise NotImplementedError

    async def write(self, path: PurePosixPath, data: bytes) -> None:
        """Replace the file's content. Its directory must already exist."""
        raise NotImplementedError

    async def mkdir(self, path: PurePosixPath) -> None:
        """Create a directory. Its parent must already exist."""
        raise NotImplementedError

    async def delete(self, path: PurePosixPath) -> None:
        """Delete a file or an empty directory."""
        raise NotImplementedError

    async def rename(self, path: PurePosixPath, target: PurePosixPath) -> None:
        """Move a file or directory to another path in the same filesystem."""
        raise NotImplementedError

    def __init_subclass__(cls, *, name: str, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not _NAME.fullmatch(name):
            raise ValueError(
                f"{cls.__name__}: filesystem name {name!r} must be lowercase letters, digits, "
                f"'-' and '_', like 'local-disk'"
            )
        config_type = next(
            (
                get_args(base)[0]
                for base in getattr(cls, "__orig_bases__", ())
                if get_origin(base) is ActivityFileSystem and get_args(base)
            ),
            None,
        )
        if not (isinstance(config_type, type) and issubclass(config_type, BaseModel)):
            raise TypeError(
                f"{cls.__name__} must subclass ActivityFileSystem[<a pydantic config model>]"
            )

        def overridden(method: str) -> bool:
            return getattr(cls, method) is not getattr(ActivityFileSystem, method)

        missing = [m for m in READ_METHODS if not overridden(m)]
        if missing:
            raise TypeError(f"{cls.__name__} does not implement {', '.join(missing)}()")
        present = [m for m in WRITE_METHODS if overridden(m)]
        if present and len(present) != len(WRITE_METHODS):
            absent = [m for m in WRITE_METHODS if m not in present]
            raise TypeError(
                f"{cls.__name__} implements {', '.join(present)}() but not "
                f"{', '.join(absent)}(); a writable filesystem needs all four"
            )
        cls.name = name
        cls.config_type = config_type
        cls.writable = bool(present)
        cls.activities = _activities(cls)

    @classmethod
    def backend(
        cls, config: C, *, timeout: timedelta = timedelta(seconds=30)
    ) -> ActivityBackend:
        """This filesystem as a mount's backend in a workflow, over ``config``.

        ``config`` is not validated here: the workflow never builds an instance, so a bad
        config fails each operation's activity instead."""
        if not isinstance(config, cls.config_type):
            raise TypeError(
                f"{cls.__name__}.backend() takes a {cls.config_type.__name__}, "
                f"got {type(config).__name__}"
            )
        source = FileSource(filesystem=cls.name, config=config.model_dump(mode="json"))
        kind = _WritableActivityBackend if cls.writable else ActivityBackend
        return kind(source, timeout)


AnyFileSystem = ActivityFileSystem[BaseModel]


async def _run(
    cls: type[AnyFileSystem],
    call: FsCall,
    operation: Callable[[AnyFileSystem, PurePosixPath], Awaitable[T]],
) -> T:
    """Build an instance from the call's config and run ``operation`` on it and the call's
    path. A rejected config is raised as a non-retryable ``ValueError``, and any ``OSError`` as
    an ``ApplicationError`` named for its built-in."""
    try:
        fs = cls(cls.config_type.model_validate(call.config))
        return await operation(fs, call.path)
    except OSError as e:
        kind = next((k.__name__ for k in _OS_ERRORS if isinstance(e, k)), "OSError")
        raise ApplicationError(
            f"{e.strerror or e}: {str(call.path)!r}", type=kind, non_retryable=True
        ) from e
    except ValueError as e:
        raise ApplicationError(
            f"{cls.name}: {e}", type="ValueError", non_retryable=True
        ) from e


def _activities(cls: type[AnyFileSystem]) -> list[Callable[..., Awaitable[object]]]:
    """The activities that run ``cls``'s methods, named ``vfs.<name>.<method>``."""

    @activity.defn(name=activity_name(cls.name, "stat"))
    async def stat(call: FsCall) -> StatResult:
        return StatResult(found=await _run(cls, call, lambda fs, path: fs.stat(path)))

    @activity.defn(name=activity_name(cls.name, "read"))
    async def read(call: FsRead) -> FileContent:
        data = await _run(cls, call, lambda fs, path: fs.read(path, call.offset, call.length))
        content, encoding = _wire(data)
        return FileContent(content=content, encoding=encoding)

    @activity.defn(name=activity_name(cls.name, "list"))
    async def list_(call: FsCall) -> list[str]:
        return await _run(cls, call, lambda fs, path: fs.list(path))

    @activity.defn(name=activity_name(cls.name, "view"))
    async def view(call: FsRead) -> FileChunk:
        length = min(call.length or MAX_VIEW_BYTES, MAX_VIEW_BYTES)
        offset = max(call.offset, 0)

        async def page(fs: AnyFileSystem, path: PurePosixPath) -> FileChunk:
            found = await fs.stat(path)
            if found is None:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
            if found.is_dir:
                raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(path))
            content, encoding = _wire((await fs.read(path, offset, length))[:length])
            return FileChunk(
                content=content,
                encoding=encoding,
                offset=offset,
                size=found.size,
                mtime=found.mtime,
            )

        return await _run(cls, call, page)

    @activity.defn(name=activity_name(cls.name, "okf_graph"))
    async def okf_graph(call: OKFGraphCall) -> OKFGraph:
        now = datetime.now(timezone.utc)
        return await _run(
            cls,
            call,
            lambda fs, path: build_okf_graph(fs, prefix=path, links=call.links, now=now),
        )

    made: list[Callable[..., Awaitable[object]]] = [stat, read, list_, view, okf_graph]
    if not cls.writable:
        return made

    @activity.defn(name=activity_name(cls.name, "write"))
    async def write(call: FsWrite) -> None:
        data = _unwire(call.content, call.encoding)
        await _run(cls, call, lambda fs, path: fs.write(path, data))

    @activity.defn(name=activity_name(cls.name, "mkdir"))
    async def mkdir(call: FsCall) -> None:
        await _run(cls, call, lambda fs, path: fs.mkdir(path))

    @activity.defn(name=activity_name(cls.name, "delete"))
    async def delete(call: FsCall) -> None:
        await _run(cls, call, lambda fs, path: fs.delete(path))

    @activity.defn(name=activity_name(cls.name, "rename"))
    async def rename(call: FsRename) -> None:
        await _run(cls, call, lambda fs, path: fs.rename(path, call.target))

    return [*made, write, mkdir, delete, rename]


# A failed filesystem operation is the script's to handle, not Temporal's to retry: the
# activities raise non-retryable errors for expected failures, and a crash gets a few tries.
_RETRY = RetryPolicy(maximum_attempts=3)


class ActivityBackend:
    """A read-only mount backend whose every operation is an ``ActivityFileSystem`` activity.
    Build it with :meth:`ActivityFileSystem.backend`."""

    def __init__(self, source: FileSource, timeout: timedelta) -> None:
        self.source = source
        self._timeout = timeout

    async def _call(self, operation: str, arg: FsCall, result_type: type[T]) -> T:
        return await workflow.execute_activity(
            activity_name(self.source.filesystem, operation),
            arg,
            result_type=result_type,
            start_to_close_timeout=self._timeout,
            retry_policy=_RETRY,
        )

    async def _do(self, operation: str, arg: FsCall) -> None:
        await workflow.execute_activity(
            activity_name(self.source.filesystem, operation),
            arg,
            start_to_close_timeout=self._timeout,
            retry_policy=_RETRY,
        )

    def _at(self, path: PurePosixPath) -> FsCall:
        return FsCall(config=self.source.config, path=path)

    async def stat(self, path: PurePosixPath) -> FileStat | None:
        return (await self._call("stat", self._at(path), StatResult)).found

    async def read(self, path: PurePosixPath) -> bytes:
        found = await self._call(
            "read", FsRead(config=self.source.config, path=path), FileContent
        )
        return _unwire(found.content, found.encoding)

    async def list(self, path: PurePosixPath) -> list[str]:
        return await self._call("list", self._at(path), list[str])

    async def okf_graph(self, prefix: PurePosixPath, *, links: bool) -> OKFGraph:
        """The OKF bundle under ``prefix``, walked in one activity (see :mod:`.okf`)."""
        return await self._call(
            "okf_graph",
            OKFGraphCall(config=self.source.config, path=prefix, links=links),
            OKFGraph,
        )


class _WritableActivityBackend(ActivityBackend):
    async def write(self, path: PurePosixPath, data: bytes) -> None:
        content, encoding = _wire(data)
        await self._do(
            "write",
            FsWrite(config=self.source.config, path=path, content=content, encoding=encoding),
        )

    async def mkdir(self, path: PurePosixPath) -> None:
        await self._do("mkdir", self._at(path))

    async def delete(self, path: PurePosixPath) -> None:
        await self._do("delete", self._at(path))

    async def rename(self, path: PurePosixPath, target: PurePosixPath) -> None:
        await self._do(
            "rename", FsRename(config=self.source.config, path=path, target=target)
        )
