"""A minimal, MODEL-FREE parent agent used only by the Code Mode filesystem end-to-end test.

Its ``code_mode_tool`` mounts three filesystems: ``/skills``, an ``InMemoryFileSystem`` seeded by
an activity; ``/workspace``, an empty writable one tracked in the ``workspace`` state; and
``/remote``, a read-only ``FileSystem`` whose every call is an activity over a store in the worker
process. Together they exercise seeding, persistence across scripts, state tracking,
activity-backed backends and error mapping across the activity boundary, under a
``WorkflowEnvironment`` with no model in the loop.

Kept in its own module for the same reason as ``_code_mode_e2e_parent``: the workflow sandbox
re-imports a workflow's defining module.
"""

from datetime import timedelta
from pathlib import PurePosixPath

from temporalio import activity, workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from ._code_mode_e2e_parent import RunCode, add_tool


_TIMEOUT = timedelta(seconds=10)

# The skill files the seed activity reads, standing in for a `skills/` directory on the worker.
SKILL_FILES = {
    "pdf/SKILL.md": "---\nname: pdf\ndescription: Work with PDFs.\n---\nUse forms.md for forms.\n",
    "pdf/forms.md": "Fill every field.\n",
}

# The store behind /remote. "ghost.txt" stats as a file but fails to read, as a backend that
# changed between two calls would.
REMOTE_FILES = {"report.txt": "Q3 revenue: 42\n", "ghost.txt": ""}


@activity.defn
async def load_skills() -> dict[str, str]:
    return dict(SKILL_FILES)


@activity.defn
async def remote_stat(path: str) -> dict[str, int | bool] | None:
    if path == ".":
        return {"is_dir": True, "size": 0}
    if path in REMOTE_FILES:
        return {"is_dir": False, "size": len(REMOTE_FILES[path])}
    return None


@activity.defn
async def remote_read(path: str) -> str:
    if path == "ghost.txt" or path not in REMOTE_FILES:
        raise ApplicationError(
            f"No such file or directory: {path!r}", type="FileNotFoundError", non_retryable=True
        )
    return REMOTE_FILES[path]


@activity.defn
async def remote_list(path: str) -> list[str]:
    return sorted(REMOTE_FILES)


VFS_ACTIVITIES = [load_skills, remote_stat, remote_read, remote_list]


class RemoteFileSystem:
    """A read-only FileSystem whose every call is an activity."""

    async def stat(self, path: PurePosixPath) -> agent.FileStat | None:
        found = await workflow.execute_activity(
            remote_stat, str(path), start_to_close_timeout=_TIMEOUT
        )
        if found is None:
            return None
        return agent.FileStat(is_dir=bool(found["is_dir"]), size=int(found["size"]))

    async def read(self, path: PurePosixPath) -> bytes:
        text = await workflow.execute_activity(
            remote_read, str(path), start_to_close_timeout=_TIMEOUT
        )
        return text.encode()

    async def list(self, path: PurePosixPath) -> list[str]:
        return await workflow.execute_activity(
            remote_list, str(path), start_to_close_timeout=_TIMEOUT
        )


@agent.defn(name="CodeModeVfsE2EParent")
class CodeModeVfsE2EParentWorkflow:
    workspace = agent.vfs_mount(
        "/workspace", agent.InMemoryFileSystem, description="Scratch space."
    )

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._run_code = agent.code_mode_tool(
            [add_tool],
            name="run_code",
            mounts=[
                agent.VFSMount(
                    "/skills",
                    agent.InMemoryFileSystem(seed=self._load_skills),
                    read_only=True,
                    description="Agent skills; read a skill's SKILL.md first.",
                ),
                self.workspace.bind(seed=None, max_bytes=10_000),
                agent.VFSMount("/remote", RemoteFileSystem(), read_only=True),
            ],
        )

    async def _load_skills(self) -> dict[str, str]:
        return await workflow.execute_activity(load_skills, start_to_close_timeout=_TIMEOUT)

    @agent.accepts
    async def run_code(self, msg: RunCode) -> TextReply:
        """Run a Python script in Code Mode over the mounted filesystems."""
        output = await self._runner.run_tool(
            str(workflow.uuid4()), self._run_code, script=msg.script
        )
        return TextReply(text=output)
