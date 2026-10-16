"""A minimal, MODEL-FREE agent used only by the Code Mode memory end-to-end test.

It builds the memory example's tool exactly as the example agent does (``okf_code_mode_tool``
over the OKF bundle at ``/memory`` on ``LocalDisk`` in the session's ``memory_dir``, with
``SkillsDisk`` at ``/skills``), and runs the scripts it is sent with it, so two sessions can be
shown to share one memory with no model in the loop.

Kept in its own module because the workflow sandbox re-imports a workflow's defining module.
"""

from pydantic import BaseModel, JsonValue
from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from examples.code_mode_memory.local_disk import (
        LocalDisk,
        LocalDiskConfig,
        SkillsConfig,
        SkillsDisk,
    )
    from examples.code_mode_memory.workflow import MemoryConfig, read_memory_index
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


class ReadIndex(BaseModel):
    """Read the memory's top-level index.md."""


@agent.defn(name="CodeModeMemoryE2EParent")
class CodeModeMemoryE2EParentWorkflow:
    skills = agent.vfs_mount("/skills", SkillsDisk, read_only=True, description="Skills.")
    memory = agent.okf_bundle_vfs_mount("/memory", LocalDisk, description="Memory.")

    @agent.init
    def __init__(self, config: AgentConfig, data: MemoryConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._memory = self.memory.bind(LocalDiskConfig(directory=data.memory_dir))
        self._run_code = agent.okf_code_mode_tool(
            self._memory,
            name="run_code",
            mounts=[self.skills.bind(SkillsConfig())],
        )

    @agent.accepts
    async def run_code(self, msg: RunCode) -> TextReply:
        """Run a Python script in Code Mode over the mounted filesystems."""
        output = await self._runner.run_tool(
            str(workflow.uuid4()), self._run_code, script=msg.script
        )
        return TextReply(text=output)

    @agent.accepts
    async def read_index(self, msg: ReadIndex) -> TextReply:
        """What the example agent puts in its system instruction as the memory index."""
        return TextReply(text=await read_memory_index(self._memory))

    @workflow.query
    def index(self) -> dict[str, JsonValue]:
        return self.memory.state.current.model_dump(mode="json")
