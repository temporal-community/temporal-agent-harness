"""Agents and tools for tests/harness/test_sandbox.py, in their own module so the workflows
run under the real sandboxed workflow runner the way an agent author's would."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio import activity, workflow
from temporalio.exceptions import ActivityError
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from agents import RunConfig, Runner
    from agents.sandbox import LocalSnapshotSpec, Manifest, SandboxAgent
    from agents.sandbox.capabilities import Capability
    from agents.sandbox.capabilities import Shell as OpenAIShell
    from agents.sandbox.entries import File
    from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
    from agents.tool import FunctionTool, Tool

    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent import Injected
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextMessage,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.sandbox import (
        Filesystem,
        IdlePolicy,
        SandboxConfig,
        SandboxSession,
        Shell,
    )

PROVIDER = "local"
# Fixed rather than per-import: the workflow sandbox re-imports this module per run.
SNAPSHOT_DIR = Path("/tmp/harness-sandbox-test-snapshots")
IDLE_AFTER = timedelta(minutes=5)

SANDBOX = SandboxConfig(
    client=PROVIDER,
    snapshot=LocalSnapshotSpec(base_path=SNAPSHOT_DIR),
    dev_mode=True,
    capabilities=[Shell(), Filesystem()],
    idle=IdlePolicy(after=IDLE_AFTER, action="persist_and_shutdown"),
)


class Notes(Capability):
    """A developer's own capability: it puts a notes file in the manifest, and its tool counts
    the lines of a workspace file through the bound session."""

    type: str = "notes"

    def process_manifest(self, manifest: Manifest) -> Manifest:
        return manifest.model_copy(
            update={"entries": {**manifest.entries, "NOTES.md": File(content=b"one\ntwo\n")}}
        )

    def tools(self) -> list[Tool]:
        session = self.session
        assert session is not None

        async def count_lines(_: Any, raw: str) -> str:
            path = json.loads(raw)["path"]
            lines = (await session.read(Path(path))).read().count(b"\n")
            return f"{path}: {lines} lines"

        return [
            FunctionTool(
                name="count_lines",
                description="Count the lines of a workspace file.",
                params_json_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "The file."}},
                    "required": ["path"],
                },
                on_invoke_tool=count_lines,
                strict_json_schema=False,
            )
        ]

    async def instructions(self, manifest: Manifest) -> str | None:
        return "Count lines with count_lines."


@agent.activity_tool_defn()
async def run_in_sandbox(session: Injected[SandboxSession], command: str) -> str:
    """Run a shell command in the sandbox and return its stdout."""
    result = await session.exec(command)
    return result.stdout.decode()


@agent.activity_tool_defn()
async def serving_task_queue(session: Injected[SandboxSession]) -> str:
    """The task queue of the worker that ran this tool."""
    assert isinstance(session, SandboxSession)
    return activity.info().task_queue


@agent.tool_defn()
async def list_workspace(session: Injected[BaseSandboxSession]) -> str:
    """List the workspace from workflow code, through the activity-backed session."""
    result = await session.exec("ls")
    return result.stdout.decode()


async def _call(runner: AgentWorkflowRunner, tool: Any, **kwargs: Any) -> str:
    try:
        return str(await runner.run_tool(str(workflow.uuid4()), tool, **kwargs))
    except Exception as e:
        cause = e.cause if isinstance(e, ActivityError) and e.cause is not None else e
        return f"error: {cause} ({getattr(e.__cause__, 'type', None) or getattr(e, 'type', None)})"


@agent.defn
class SandboxProbeAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            sandbox=SANDBOX,
        )
        self._tools = {t.__name__: t for t in self._runner.sandbox_tools()}

    @agent.accepts
    async def write_files(self, message: TextMessage) -> TextReply:
        """Exercise every way a tool reaches the sandbox."""
        r = self._runner
        exec_command = self._tools["exec_command"]
        apply_patch = self._tools["apply_patch"]
        view_image = self._tools["view_image"]
        results = {
            "custom": await _call(
                r, run_in_sandbox, command=f"printf '{message.text}' > a.txt && cat a.txt"
            ),
            "exec_command": await _call(r, exec_command, cmd="cat a.txt"),
            "apply_patch": await _call(
                r,
                apply_patch,
                patch="*** Begin Patch\n*** Add File: b.txt\n+bee\n*** End Patch\n",
            ),
            "png": await _call(
                r, exec_command, cmd="printf '\\211PNG\\r\\n\\032\\n0000' > img.png"
            ),
            "view_image": await _call(r, view_image, path="img.png"),
            "inline": await _call(r, list_workspace),
        }
        return TextReply(text=repr(results))

    @agent.accepts
    async def shell(self, message: TextMessage) -> TextReply:
        """Call exec_command, or write_stdin when the arguments name a session, with the
        message's JSON as the arguments."""
        args = json.loads(message.text)
        tool = self._tools["write_stdin" if "session_id" in args else "exec_command"]
        return TextReply(text=await _call(self._runner, tool, **args))

    @agent.accepts
    async def read_file(self, message: TextMessage) -> TextReply:
        """Read a workspace file back through a custom tool."""
        return TextReply(text=await _call(self._runner, run_in_sandbox, command=f"cat {message.text}"))

    @agent.accepts
    async def bad_patches(self, message: TextMessage) -> TextReply:
        """Send apply_patch two malformed patches the model could plausibly write."""
        apply_patch = self._tools["apply_patch"]
        results = {
            "unprefixed_line": await _call(
                self._runner,
                apply_patch,
                patch="*** Begin Patch\n*** Add File: c.txt\nsea\n*** End Patch",
            ),
            "no_begin_marker": await _call(
                self._runner, apply_patch, patch="*** Add File: c.txt\n+sea\n*** End Patch"
            ),
        }
        return TextReply(text=repr(results))

    @agent.accepts
    async def repairable_patches(self, message: TextMessage) -> TextReply:
        """Send apply_patch the Add File mistakes it repairs, and read back what they made."""
        r = self._runner
        apply_patch = self._tools["apply_patch"]
        results = {
            "plus_end_marker": await _call(
                r,
                apply_patch,
                patch="*** Begin Patch\n*** Add File: c.txt\n+sea\n+*** End Patch",
            ),
            "empty_files": await _call(
                r,
                apply_patch,
                patch=(
                    "*** Begin Patch\n*** Add File: pkg/__init__.py\n"
                    "*** Add File: pkg/mod.py\n+x = 1\n*** Add File: empty.txt\n*** End Patch"
                ),
            ),
            "contents": await _call(
                r,
                run_in_sandbox,
                command="wc -c < pkg/__init__.py; wc -c < empty.txt; cat c.txt; echo; cat pkg/mod.py",
            ),
        }
        return TextReply(text=repr(results))

    @agent.accepts
    async def where(self, message: TextMessage) -> TextReply:
        """Report which worker task queue served a sandbox tool."""
        return TextReply(text=await _call(self._runner, serving_task_queue))

    @agent.accepts
    async def instructions(self, message: TextMessage) -> TextReply:
        """The capability instructions and tool names the runner offers."""
        names = [t.__name__ for t in self._runner.sandbox_tools()]
        return TextReply(text=f"{names}\n{self._runner.sandbox_instructions()}")


@agent.defn
class NoSandboxAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @agent.accepts
    async def try_tool(self, message: TextMessage) -> TextReply:
        """Call a sandbox tool on an agent that has no sandbox."""
        return TextReply(text=await _call(self._runner, run_in_sandbox, command="true"))


@agent.defn
class PinnedProbeAgent:
    """An agent whose sandbox gives up on an unresponsive pinned worker after two seconds."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            sandbox=dataclasses.replace(SANDBOX, affinity_timeout=timedelta(seconds=2)),
        )

    @agent.accepts
    async def where(self, message: TextMessage) -> TextReply:
        """Report which worker task queue served a sandbox tool."""
        return TextReply(text=await _call(self._runner, serving_task_queue))


@agent.defn
class OpenAISandboxAgent:
    """Runs an OpenAI SandboxAgent each turn, on the runner's own sandbox."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            sandbox=SANDBOX,
        )

    @agent.accepts
    async def run_agent(self, message: TextMessage) -> TextReply:
        """One SandboxAgent run."""
        result = await Runner.run(
            SandboxAgent(
                name="coder", instructions="Use the shell.", capabilities=[OpenAIShell()]
            ),
            message.text,
            run_config=RunConfig(sandbox=await self._runner.sandbox_run_config()),
        )
        return TextReply(text=str(result.final_output))

    @agent.accepts
    async def read_file(self, message: TextMessage) -> TextReply:
        """Read a workspace file back through a custom tool."""
        return TextReply(text=await _call(self._runner, run_in_sandbox, command=f"cat {message.text}"))


@agent.defn
class NotesAgent:
    """An agent whose sandbox has only a developer's own capability."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            sandbox=dataclasses.replace(SANDBOX, capabilities=[Notes()]),
        )

    @agent.accepts
    async def count(self, message: TextMessage) -> TextReply:
        """The runner's tool names and instructions, then count_lines on the message's path."""
        tools = {t.__name__: t for t in self._runner.sandbox_tools()}
        counted = await _call(self._runner, tools["count_lines"], path=message.text)
        return TextReply(
            text=repr(
                {
                    "names": sorted(tools),
                    "instructions": self._runner.sandbox_instructions(),
                    "counted": counted,
                }
            )
        )
