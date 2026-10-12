"""``agent.vfs_mount(...)``: a Code Mode mount declared on the agent class, so the console can show it.

The declaration fixes what is static about a mount: its path, description, access and backend
type. It is observable state, like ``agent.state(...)``: a :class:`~.vfs.FileTree` for an
:class:`~.vfs.InMemoryFileSystem`, a :class:`~.vfs.FileIndex` for an
:class:`~.activity_fs.ActivityFileSystem`, published under the attribute name and starting with
the mount's details. What depends on the session (a seed, a config) is given per instance with
``bind(...)``, which returns the :class:`~.vfs.VFSMount` a ``code_mode_tool`` takes::

    class MyAgent:
        skills = agent.vfs_mount("/skills", agent.InMemoryFileSystem, read_only=True,
                                 description="Skills, one folder each.")
        memory = agent.vfs_mount("/memory", LocalDisk, description="Your memory.")

        @agent.init
        def __init__(self, config: AgentConfig, data: MyData) -> None:
            self._runner = AgentWorkflowRunner(config, ...)
            self._code_tool = agent.code_mode_tool([], name="run_code", mounts=[
                self.skills.bind(seed=self._load_skills),
                self.memory.bind(LocalDiskConfig(directory=data.memory_dir)),
            ])

``bind`` writes nothing to the state. The state changes only as scripts use the mount, inside
a turn.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Generic, TypeVar, overload

from pydantic import BaseModel
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.state import StateRef
    from temporal_agent_harness.harness.state.decl import StateDecl, state_ref

    from .activity_fs import ActivityFileSystem
    from .okf import OKF_VERSION
    from .vfs import (
        FileIndex,
        FileTree,
        InMemoryFileSystem,
        IndexedVFSMount,
        Seed,
        VFSMount,
        check_mount_path,
    )

__all__ = [
    "ActivityVFSMountDecl",
    "InMemoryVFSMountDecl",
    "UnboundActivityVFSMount",
    "UnboundInMemoryVFSMount",
    "okf_bundle_vfs_mount",
    "vfs_mount",
]

C = TypeVar("C", bound=BaseModel)

# Where an instance keeps its unbound mounts, keyed by state id; on the instance for the same
# reason declared state keeps its refs there (see ``state.decl``).
_MOUNTS_ATTR = "__harness_vfs_mounts__"


class _Unbound:
    """A declared mount on one agent instance, until ``bind`` makes it a :class:`VFSMount`."""

    def __init__(self, decl: _VFSMountDecl[Any], obj: object) -> None:
        self._decl = decl
        self._obj = obj
        self._bound = False

    def _claim(self) -> None:
        if self._bound:
            raise RuntimeError(
                f"vfs_mount {self._decl.state_id!r} ({self._decl.path}) is already bound; bind "
                "it once and pass the same VFSMount to every tool that uses it"
            )
        self._bound = True

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._decl.state_id!r}, {self._decl.path!r})"


class UnboundInMemoryVFSMount(_Unbound):
    """An ``agent.vfs_mount(...)`` over an :class:`InMemoryFileSystem` on one agent instance."""

    @property
    def state(self) -> StateRef[FileTree]:
        """The mount's files, as published."""
        return state_ref(self._obj, self._decl)

    def bind(self, *, seed: Seed | None, max_bytes: int | None = None) -> VFSMount:
        """This instance's mount. ``seed`` loads the initial files before the first operation
        (see :class:`InMemoryFileSystem`); pass ``seed=None`` for a mount that starts empty.
        ``max_bytes`` caps the total size of the files."""
        self._claim()
        decl = self._decl
        return VFSMount(
            decl.path,
            InMemoryFileSystem._in_state(self.state, seed=seed, max_bytes=max_bytes),
            read_only=decl.read_only,
            description=decl.description,
        )


class UnboundActivityVFSMount(_Unbound, Generic[C]):
    """An ``agent.vfs_mount(...)`` over an :class:`ActivityFileSystem` on one agent instance."""

    @property
    def state(self) -> StateRef[FileIndex]:
        """What the session has seen of the mount's tree, as published."""
        return state_ref(self._obj, self._decl)

    def bind(self, config: C, *, timeout: timedelta = timedelta(seconds=30)) -> VFSMount:
        """This instance's mount, over the filesystem built from ``config``. ``timeout`` bounds
        each operation's activity."""
        decl = self._decl
        assert decl.filesystem is not None
        backend = decl.filesystem.backend(config, timeout=timeout)
        self._claim()
        return IndexedVFSMount(
            decl.path,
            backend,
            read_only=decl.read_only,
            description=decl.description,
            index=self.state,
            source=backend.source,
        )


S = TypeVar("S", FileTree, FileIndex)
U = TypeVar("U", bound=_Unbound)


class _VFSMountDecl(StateDecl[S], Generic[S, U]):
    """A mount declared on an agent class. Read from the class, it is this declaration; read
    from an instance, it is that instance's unbound mount."""

    __slots__ = ("path", "description", "read_only", "filesystem", "_unbound")

    def __init__(
        self,
        state_type: type[S],
        unbound: type[U],
        path: str,
        *,
        description: str,
        read_only: bool,
        filesystem: type[ActivityFileSystem[Any]] | None = None,
        okf_version: str | None = None,
    ) -> None:
        check_mount_path(path)
        header: dict[str, object] = {
            "mount": path,
            "description": description,
            "read_only": read_only,
        }
        if okf_version is not None:
            header["okf_version"] = okf_version
        super().__init__(state_type, initial=lambda: state_type.model_validate(header))
        self.path = path
        self.description = description
        self.read_only = read_only
        self.filesystem = filesystem
        self._unbound = unbound

    @overload
    def __get__(self, obj: None, owner: type | None = None) -> _VFSMountDecl[S, U]: ...
    @overload
    def __get__(self, obj: object, owner: type | None = None) -> U: ...
    def __get__(self, obj: object | None, owner: type | None = None) -> Any:
        if obj is None:
            return self
        try:
            mounts: dict[str, _Unbound] = vars(obj).setdefault(_MOUNTS_ATTR, {})
        except TypeError:
            raise TypeError(
                f"{type(obj).__name__} declares a vfs_mount but its instances have no "
                "__dict__ (__slots__); declared mounts are stored on the instance"
            ) from None
        found = mounts.get(self.state_id)
        if found is None:
            found = mounts[self.state_id] = self._unbound(self, obj)
        return found

    def __set__(self, obj: object, value: object) -> None:
        raise AttributeError(
            f"{self.state_id!r} is a declared vfs_mount and cannot be reassigned; bind it with "
            f"`self.{self.state_id}.bind(...)`"
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._state_id!r}, {self.path!r})"


class InMemoryVFSMountDecl(_VFSMountDecl[FileTree, UnboundInMemoryVFSMount]):
    """``agent.vfs_mount(path, InMemoryFileSystem, ...)``: its files are a :class:`FileTree`."""

    __slots__ = ()


class ActivityVFSMountDecl(_VFSMountDecl[FileIndex, UnboundActivityVFSMount[C]], Generic[C]):
    """``agent.vfs_mount(path, SomeActivityFileSystem, ...)``: what the session has seen of its
    tree is a :class:`FileIndex`."""

    __slots__ = ()


@overload
def vfs_mount(
    path: str,
    backend: type[InMemoryFileSystem],
    *,
    description: str,
    read_only: bool = False,
) -> InMemoryVFSMountDecl: ...
@overload
def vfs_mount(
    path: str,
    backend: type[ActivityFileSystem[C]],
    *,
    description: str,
    read_only: bool = False,
) -> ActivityVFSMountDecl[C]: ...
def vfs_mount(
    path: str,
    backend: type[InMemoryFileSystem] | type[ActivityFileSystem[Any]],
    *,
    description: str,
    read_only: bool = False,
) -> InMemoryVFSMountDecl | ActivityVFSMountDecl[Any]:
    """Declare a Code Mode mount on an agent class; the attribute name is its state id.

    ``backend`` is the filesystem class: :class:`InMemoryFileSystem` or an
    :class:`ActivityFileSystem` subclass. ``description`` is shown to the model next to the
    mount. In ``@agent.init``, ``self.<name>.bind(...)`` returns the instance's
    :class:`VFSMount` to pass to ``code_mode_tool(mounts=...)``.
    """
    if backend is InMemoryFileSystem:
        return InMemoryVFSMountDecl(
            FileTree,
            UnboundInMemoryVFSMount,
            path,
            description=description,
            read_only=read_only,
        )
    if isinstance(backend, type) and issubclass(backend, ActivityFileSystem):
        return _activity_decl("vfs_mount", path, backend, description, read_only, None)
    raise TypeError(
        f"vfs_mount({path!r}): the backend must be InMemoryFileSystem or an "
        f"ActivityFileSystem subclass, got {backend!r}"
    )


def okf_bundle_vfs_mount(
    path: str,
    backend: type[ActivityFileSystem[C]],
    *,
    description: str,
    read_only: bool = False,
) -> ActivityVFSMountDecl[C]:
    """Declare a Code Mode mount whose files form an OKF bundle (see :mod:`.okf`).

    Like :func:`vfs_mount` over an :class:`ActivityFileSystem`, with the same ``bind``. The
    mount's ``FileIndex`` records the OKF version, which is how the console knows to offer the
    bundle's graph. Pass the bound mount to ``okf_code_mode_tool`` to give scripts the OKF host
    functions and explain the format to the model.
    """
    if not (isinstance(backend, type) and issubclass(backend, ActivityFileSystem)):
        raise TypeError(
            f"okf_bundle_vfs_mount({path!r}): the backend must be an ActivityFileSystem "
            f"subclass, got {backend!r}"
        )
    return _activity_decl("okf_bundle_vfs_mount", path, backend, description, read_only, OKF_VERSION)


def _activity_decl(
    what: str,
    path: str,
    backend: type[ActivityFileSystem[Any]],
    description: str,
    read_only: bool,
    okf_version: str | None,
) -> ActivityVFSMountDecl[Any]:
    if not read_only and not backend.writable:
        raise TypeError(
            f"{what}({path!r}): {backend.__name__} is read-only (it implements no "
            "write methods); declare the mount read_only=True"
        )
    return ActivityVFSMountDecl(
        FileIndex,
        UnboundActivityVFSMount,
        path,
        description=description,
        read_only=read_only,
        filesystem=backend,
        okf_version=okf_version,
    )
