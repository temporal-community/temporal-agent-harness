"""A read-only ``ActivityFileSystem`` over a git repository, for a mount whose files are kept
in git: documentation, skills, or a knowledge base that several agents and people share.

The worker says which repositories exist. A mount's config picks one of them by its key, never
by URL, because a config is untrusted input::

    KnowledgeBase = git_repository_filesystem(
        "knowledge-base",
        repositories={"kb": "https://github.com/acme/knowledge-base.git"},
    )

    # workflow
    kb = agent.vfs_mount("/kb", KnowledgeBase, read_only=True, description="...")
    self.kb.bind(GitRepositoryConfig(repository="kb", ref="main", subdirectory="wiki"))

    # worker
    AgentHarnessPlugin(filesystems=[KnowledgeBase])

Each worker keeps a bare clone of a repository in its cache directory and fetches it again when
the last fetch is older than ``refresh_after``. A file is read from git's object store at the
commit the ref points to, so no working tree is checked out. git runs with the worker's own
configuration, which is where credentials for a private repository belong.
"""

from __future__ import annotations

import asyncio
import errno
import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from temporal_agent_harness.harness.code_mode.activity_fs import ActivityFileSystem
from temporal_agent_harness.harness.code_mode.vfs import FileStat

_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_LAST_FETCH = "harness-last-fetch"

# One fetch or clone of a repository at a time in this process.
_locks: dict[Path, asyncio.Lock] = {}


class GitRepositoryConfig(BaseModel):
    """Which tree of which repository a mount shows."""

    repository: str = Field(description="A key of the worker's `repositories`.")
    ref: str = Field(default="HEAD", description="A branch, a tag, or a full commit id.")
    subdirectory: PurePosixPath = Field(
        default=PurePosixPath("."), description="The directory of the repository to mount."
    )


@dataclass(frozen=True)
class _Entry:
    is_dir: bool
    object_id: str
    size: int


async def _git(*args: str, cwd: Path | None = None) -> bytes:
    """Run git and return its output. A failure raises ``RuntimeError`` with git's message."""
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # git must fail on a missing credential, not wait for someone to type one.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    out, err = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {err.decode(errors='replace').strip()}")
    return out


async def _clone(url: str, mirror: Path) -> None:
    mirror.parent.mkdir(parents=True, exist_ok=True)
    # Cloned beside the mirror and then renamed, so another worker never sees half a clone.
    scratch = Path(tempfile.mkdtemp(prefix=f"{mirror.name}.", dir=mirror.parent))
    try:
        await _git("clone", "--bare", "--quiet", "--", url, str(scratch))
        (scratch / _LAST_FETCH).touch()
        try:
            os.rename(scratch, mirror)
        except OSError:
            if not mirror.is_dir():
                raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def _refresh(url: str, mirror: Path, refresh_after: timedelta | None) -> None:
    """Clone the repository if this worker has no mirror, or fetch it if the mirror is old.
    With no ``refresh_after`` it is always fetched."""
    async with _locks.setdefault(mirror, asyncio.Lock()):
        if not mirror.is_dir():
            await _clone(url, mirror)
            return
        stamp = mirror / _LAST_FETCH
        try:
            age = time.time() - stamp.stat().st_mtime
        except FileNotFoundError:
            age = None
        if refresh_after is not None and age is not None:
            if age < refresh_after.total_seconds():
                return
        await _git(
            "fetch", "--quiet", "--prune", "--force", "--", url,
            "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*",
            cwd=mirror,
        )  # fmt: skip
        stamp.touch()


class _GitRepository:
    """The read methods of a git-backed filesystem. :func:`git_repository_filesystem` makes the
    class a worker registers."""

    _repositories: Mapping[str, str]
    _cache_dir: Path
    _refresh_after: timedelta

    def __init__(self, config: GitRepositoryConfig) -> None:
        super().__init__(config)  # type: ignore[call-arg]
        if config.repository not in self._repositories:
            raise ValueError(f"unknown repository {config.repository!r}")
        if not _REF.fullmatch(config.ref) or ".." in config.ref:
            raise ValueError(f"ref {config.ref!r} is not a branch, a tag or a commit id")
        subdirectory = config.subdirectory
        if subdirectory.is_absolute() or ".." in subdirectory.parts:
            raise ValueError(f"subdirectory '{subdirectory}' must be a path inside the repository")
        self._url = self._repositories[config.repository]
        self._mirror = self._cache_dir / f"{config.repository}.git"
        self._ref = config.ref
        self._subdirectory = subdirectory
        self._entries: dict[PurePosixPath, _Entry] | None = None
        self._mtime = 0.0

    async def _commit(self) -> str:
        """The commit the ref points to. A full commit id that the mirror already has needs no
        fetch, since it can never point to anything else."""
        verify = ("rev-parse", "--verify", "--quiet", "--end-of-options", f"{self._ref}^{{commit}}")
        pinned = _COMMIT.fullmatch(self._ref) is not None
        if pinned and self._mirror.is_dir():
            try:
                return (await _git(*verify, cwd=self._mirror)).decode().strip()
            except RuntimeError:
                pass
        # A commit id that the mirror lacks is new, so the age of the mirror says nothing.
        await _refresh(self._url, self._mirror, None if pinned else self._refresh_after)
        try:
            return (await _git(*verify, cwd=self._mirror)).decode().strip()
        except RuntimeError:
            raise ValueError(f"ref {self._ref!r} is not in the repository") from None

    async def _tree(self) -> dict[PurePosixPath, _Entry]:
        """Every path under the subdirectory, read once for each instance."""
        if self._entries is not None:
            return self._entries
        commit = await self._commit()
        whole = self._subdirectory == PurePosixPath(".")
        root = commit if whole else f"{commit}:{self._subdirectory}"
        try:
            listing = await _git("ls-tree", "-r", "-t", "-l", "-z", root, cwd=self._mirror)
        except RuntimeError:
            raise ValueError(
                f"subdirectory '{self._subdirectory}' is not a directory at {self._ref!r}"
            ) from None
        entries = {PurePosixPath("."): _Entry(is_dir=True, object_id="", size=0)}
        for line in listing.split(b"\0"):
            if not line:
                continue
            meta, _, name = line.partition(b"\t")
            mode, kind, object_id, size = meta.decode().split()
            # A submodule has no content here, and a symlink could point out of the mount.
            if kind == "commit" or mode == "120000":
                continue
            entries[PurePosixPath(name.decode(errors="surrogateescape"))] = _Entry(
                is_dir=kind == "tree", object_id=object_id, size=0 if kind == "tree" else int(size)
            )
        self._mtime = float(await _git("show", "-s", "--format=%ct", commit, cwd=self._mirror))
        self._entries = entries
        return entries

    async def stat(self, path: PurePosixPath) -> FileStat | None:
        entry = (await self._tree()).get(path)
        if entry is None:
            return None
        return FileStat(is_dir=entry.is_dir, size=entry.size, mtime=self._mtime)

    async def read(
        self, path: PurePosixPath, offset: int = 0, length: int | None = None
    ) -> bytes:
        entry = (await self._tree()).get(path)
        if entry is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
        if entry.is_dir:
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(path))
        data = await _git("cat-file", "blob", entry.object_id, cwd=self._mirror)
        return data[offset:] if length is None else data[offset : offset + length]

    async def list(self, path: PurePosixPath) -> list[str]:
        entries = await self._tree()
        entry = entries.get(path)
        if entry is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
        if not entry.is_dir:
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(path))
        return sorted(p.name for p in entries if p != path and p.parent == path)


def default_git_cache_dir() -> Path:
    """Where a worker keeps its clones when it is given no ``cache_dir``."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "temporal-agent-harness" / "git"


def git_repository_filesystem(
    name: str,
    *,
    repositories: Mapping[str, str],
    cache_dir: Path | None = None,
    refresh_after: timedelta = timedelta(minutes=5),
) -> type[ActivityFileSystem[GitRepositoryConfig]]:
    """A read-only filesystem named ``name`` over the git repositories in ``repositories``.

    ``repositories`` maps the key a config uses to the URL git clones from. ``cache_dir`` is
    where this worker keeps its bare clones. A branch or tag is fetched again once the last
    fetch is older than ``refresh_after``; a full commit id that the clone has is never fetched.
    """
    for key in repositories:
        if not _KEY.fullmatch(key):
            raise ValueError(
                f"repository key {key!r} must be letters, digits, '.', '-' and '_', like 'docs'"
            )
    if refresh_after < timedelta(0):
        raise ValueError("refresh_after must not be negative")

    class GitRepository(_GitRepository, ActivityFileSystem[GitRepositoryConfig], name=name):
        _repositories = dict(repositories)
        _cache_dir = (cache_dir or default_git_cache_dir()).resolve()
        _refresh_after = refresh_after

    return GitRepository
