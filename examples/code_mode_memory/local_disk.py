"""The example's two ``ActivityFileSystem``\\ s, over directories of the worker's local disk.

- ``LocalDisk``: the agent's memory, a directory inside this example's ``memories/`` folder named
  by the session's config. Writable.
- ``SkillsDisk``: this example's ``skills/`` folder. Read-only: it implements no write methods,
  so no activity can change it, whatever config is sent.

Their methods are plain async code that touches the disk; the harness runs each one as an
activity (``vfs.local-disk.stat``, ``vfs.skills-disk.read``, ...) on whichever worker registers
it with ``AgentHarnessPlugin(filesystems=[LocalDisk, SkillsDisk])``, and the console's file
viewer calls ``vfs.<name>.view`` the same way.

A config is untrusted input: anyone who can reach the file viewer chooses one. So the only
directories ``LocalDisk`` accepts are relative paths inside this example's ``memories/``
folder, and every path a call names must resolve inside its directory.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from temporal_agent_harness.harness import agent

MEMORIES_DIR = Path(__file__).parent / "memories"
SKILLS_DIR = Path(__file__).parent / "skills"


class _Directory:
    """Reading a directory on disk: every path a call names must resolve inside ``_root``."""

    _root: Path

    def _on_disk(self, path: PurePosixPath) -> Path:
        resolved = (self._root / path).resolve()
        if resolved != self._root and self._root not in resolved.parents:
            raise PermissionError(errno.EACCES, "Path is outside the directory", str(path))
        return resolved

    async def stat(self, path: PurePosixPath) -> agent.FileStat | None:
        target = self._on_disk(path)
        try:
            found = target.stat()
        except FileNotFoundError:
            return None
        is_dir = target.is_dir()
        return agent.FileStat(
            is_dir=is_dir, size=0 if is_dir else found.st_size, mtime=found.st_mtime
        )

    async def read(
        self, path: PurePosixPath, offset: int = 0, length: int | None = None
    ) -> bytes:
        with self._on_disk(path).open("rb") as f:
            f.seek(offset)
            return f.read(-1 if length is None else length)

    async def list(self, path: PurePosixPath) -> list[str]:
        return sorted(os.listdir(self._on_disk(path)))


class SkillsConfig(BaseModel):
    """No settings: the skills folder is fixed."""


class SkillsDisk(_Directory, agent.ActivityFileSystem[SkillsConfig], name="skills-disk"):
    def __init__(self, config: SkillsConfig) -> None:
        super().__init__(config)
        self._root = SKILLS_DIR.resolve()


class LocalDiskConfig(BaseModel):
    """Which memory directory to use, relative to this example's ``memories/`` folder."""

    directory: PurePosixPath = Field(description="e.g. 'alice' or 'team/shared'")


class LocalDisk(_Directory, agent.ActivityFileSystem[LocalDiskConfig], name="local-disk"):
    def __init__(self, config: LocalDiskConfig) -> None:
        super().__init__(config)
        relative = config.directory
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"directory '{relative}' must be a relative path inside memories/")
        base = MEMORIES_DIR.resolve()
        root = (base / relative).resolve()
        if base not in root.parents:
            raise ValueError(f"directory '{relative}' is not inside memories/")
        root.mkdir(parents=True, exist_ok=True)
        self._root = root

    async def write(self, path: PurePosixPath, data: bytes) -> None:
        self._on_disk(path).write_bytes(data)

    async def mkdir(self, path: PurePosixPath) -> None:
        self._on_disk(path).mkdir()

    async def delete(self, path: PurePosixPath) -> None:
        target = self._on_disk(path)
        if target == self._root:
            raise PermissionError(errno.EACCES, "Cannot delete the memory directory", str(path))
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()

    async def rename(self, path: PurePosixPath, target: PurePosixPath) -> None:
        source = self._on_disk(path)
        if source == self._root:
            raise PermissionError(errno.EACCES, "Cannot move the memory directory", str(path))
        source.rename(self._on_disk(target))
