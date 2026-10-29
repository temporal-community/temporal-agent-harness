"""A test ``ActivityFileSystem`` over an in-process store, and a MODEL-FREE agent that mounts it.

Kept in its own module because the workflow sandbox re-imports a workflow's defining module.
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from pydantic import BaseModel, JsonValue
from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


@dataclass
class Store:
    files: dict[str, bytes] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)


# The stores the filesystem's activities work on, by name. They live in the worker process.
STORES: dict[str, Store] = {}


class StoreConfig(BaseModel):
    store: str


def _error(kind: type[OSError], code: int, path: PurePosixPath) -> OSError:
    return kind(code, os.strerror(code), str(path))


class StoreFileSystem(agent.ActivityFileSystem[StoreConfig], name="test-store"):
    def __init__(self, config: StoreConfig) -> None:
        super().__init__(config)
        if config.store not in STORES:
            raise ValueError(f"no store named {config.store!r}")
        self._store = STORES[config.store]

    def _is_dir(self, path: PurePosixPath) -> bool:
        return str(path) == "." or str(path) in self._store.dirs

    async def stat(self, path: PurePosixPath) -> agent.FileStat | None:
        if self._is_dir(path):
            return agent.FileStat(is_dir=True)
        data = self._store.files.get(str(path))
        return None if data is None else agent.FileStat(is_dir=False, size=len(data), mtime=1.5)

    async def read(
        self, path: PurePosixPath, offset: int = 0, length: int | None = None
    ) -> bytes:
        data = self._store.files.get(str(path))
        if data is None:
            raise _error(FileNotFoundError, errno.ENOENT, path)
        return data[offset : None if length is None else offset + length]

    async def list(self, path: PurePosixPath) -> list[str]:
        prefix = "" if str(path) == "." else f"{path}/"
        children = {
            p[len(prefix) :].split("/")[0]
            for p in [*self._store.files, *self._store.dirs]
            if p.startswith(prefix)
        }
        return sorted(children)

    async def write(self, path: PurePosixPath, data: bytes) -> None:
        if not self._is_dir(path.parent):
            raise _error(FileNotFoundError, errno.ENOENT, path)
        self._store.files[str(path)] = data

    async def mkdir(self, path: PurePosixPath) -> None:
        self._store.dirs.add(str(path))

    async def delete(self, path: PurePosixPath) -> None:
        if self._store.files.pop(str(path), None) is None:
            self._store.dirs.discard(str(path))

    async def rename(self, path: PurePosixPath, target: PurePosixPath) -> None:
        self._store.files[str(target)] = self._store.files.pop(str(path))


class ReadOnlyStoreFileSystem(agent.ActivityFileSystem[StoreConfig], name="test-store-ro"):
    def __init__(self, config: StoreConfig) -> None:
        super().__init__(config)
        self._inner = StoreFileSystem(config)

    async def stat(self, path: PurePosixPath) -> agent.FileStat | None:
        return await self._inner.stat(path)

    async def read(
        self, path: PurePosixPath, offset: int = 0, length: int | None = None
    ) -> bytes:
        return await self._inner.read(path, offset, length)

    async def list(self, path: PurePosixPath) -> list[str]:
        return await self._inner.list(path)


class RunScript(BaseModel):
    """A script to run in Code Mode."""

    script: str


class StoreAgentData(BaseModel):
    """Which store the agent mounts."""

    store: str


@agent.defn(name="ActivityFsParent")
class ActivityFsParentWorkflow:
    files = agent.vfs_mount("/store", StoreFileSystem, description="The test store.")

    @agent.init
    def __init__(self, config: AgentConfig, data: StoreAgentData) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._run_code = agent.code_mode_tool(
            [],
            name="run_code",
            mounts=[self.files.bind(StoreConfig(store=data.store))],
        )

    @agent.accepts
    async def run_code(self, msg: RunScript) -> TextReply:
        """Run a Python script in Code Mode over the store."""
        output = await self._runner.run_tool(
            str(workflow.uuid4()), self._run_code, script=msg.script
        )
        return TextReply(text=output)

    @workflow.query
    def index(self) -> dict[str, JsonValue]:
        return self.files.state.current.model_dump(mode="json")


@agent.defn(name="OKFParent")
class OKFParentWorkflow:
    knowledge = agent.okf_bundle_vfs_mount("/kb", StoreFileSystem, description="Notes.")

    @agent.init
    def __init__(self, config: AgentConfig, data: StoreAgentData) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._run_code = agent.okf_code_mode_tool(
            self.knowledge.bind(StoreConfig(store=data.store)), name="run_code"
        )

    @agent.accepts
    async def run_code(self, msg: RunScript) -> TextReply:
        """Run a Python script in Code Mode over the bundle."""
        output = await self._runner.run_tool(
            str(workflow.uuid4()), self._run_code, script=msg.script
        )
        return TextReply(text=output)

    @workflow.query
    def index(self) -> dict[str, JsonValue]:
        return self.knowledge.state.current.model_dump(mode="json")
