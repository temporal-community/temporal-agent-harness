"""A minimal, MODEL-FREE agent used only by the Code Mode memory end-to-end test.

It mounts the memory example's two filesystems exactly as the example agent does (the
activity-seeded ``/skills`` and ``LocalDisk`` at ``/memory`` over the session's ``memory_dir``,
indexed), and runs the scripts it is sent over them, so two sessions can be shown to share one
memory with no model in the loop.

Kept in its own module because the workflow sandbox re-imports a workflow's defining module.
"""

from datetime import timedelta

from pydantic import BaseModel, JsonValue
from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from examples.code_mode_memory.activities import load_skills
    from examples.code_mode_memory.local_disk import LocalDisk, LocalDiskConfig
    from examples.code_mode_memory.workflow import MemoryConfig
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


class RunCode(BaseModel):
    """A script to run in Code Mode."""

    script: str


@agent.defn(name="CodeModeMemoryE2EParent")
class CodeModeMemoryE2EParentWorkflow:
    memory = agent.vfs_mount("/memory", LocalDisk, description="Memory.")

    @agent.init
    def __init__(self, config: AgentConfig, data: MemoryConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._run_code = agent.code_mode_tool(
            [],
            name="run_code",
            mounts=[
                agent.VFSMount(
                    "/skills", agent.InMemoryFileSystem(seed=self._load_skills), read_only=True
                ),
                self.memory.bind(LocalDiskConfig(directory=data.memory_dir)),
            ],
        )

    async def _load_skills(self) -> dict[str, str]:
        return await workflow.execute_activity(
            load_skills, start_to_close_timeout=timedelta(seconds=10)
        )

    @agent.accepts
    async def run_code(self, msg: RunCode) -> TextReply:
        """Run a Python script in Code Mode over the mounted filesystems."""
        output = await self._runner.run_tool(
            str(workflow.uuid4()), self._run_code, script=msg.script
        )
        return TextReply(text=output)

    @workflow.query
    def index(self) -> dict[str, JsonValue]:
        return self.memory.state.current.model_dump(mode="json")
