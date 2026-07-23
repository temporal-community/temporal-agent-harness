# ABOUTME: A sandboxed-tool agent whose workflow module wraps EVERY temporal_agent_harness import
# (agent, AgentWorkflowRunner, AND agent_protocol) in one `imports_passed_through()` block — kept
# separate from test_sandboxed_tools.py's other fixtures/imports specifically so this module is
# reachable exactly as Temporal's REAL (default) SandboxedWorkflowRunner would load it: fresh,
# from scratch, independent of how the test file that references it happened to import things.
# See test_sandboxed_real_sandboxed_workflow_runner in test_sandboxed_tools.py — the one test in
# this suite that does NOT use UnsandboxedWorkflowRunner, because two real bugs (an `os.environ`
# read that's restricted under real sandboxed execution, and a module-import-order bug that
# silently splits agent_workflow.py into two copies with two different `_CURRENT_RUNNER`
# contextvars) were both invisible under UnsandboxedWorkflowRunner and only reproduced here.

from pathlib import Path

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel
    from remote import Subprocess

    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent_protocol import AgentConfig, TextMessage, TextReply
    from temporal_agent_harness.harness.sandbox import SandboxConfig


class RealSandboxInput(BaseModel):
    pass


class RealSandboxResult(BaseModel):
    ok: bool = True


@agent.activity_tool_defn(sandboxed=True)
async def real_sandbox_probe(arg: RealSandboxInput) -> RealSandboxResult:
    """A sandboxed tool with no observable side effect beyond succeeding — this test only cares
    whether dispatch() reaches it at all under real sandboxed workflow execution."""
    return RealSandboxResult()


SANDBOX = SandboxConfig(backend=Subprocess(), local_project_root=Path(__file__).parent)


@workflow.defn(name="RealSandboxedWorkflowRunnerProbeAgent")
@agent.defn
class RealSandboxedWorkflowRunnerProbeAgent:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=agent.ToolApprovalPolicy.dangerously_skip_all(),
            sandbox=SANDBOX,
        )

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def probe(self, message: TextMessage) -> TextReply:
        """Run the sandboxed tool once."""
        await self._runner.run_tool("call-1", real_sandbox_probe, RealSandboxInput())
        return TextReply(text="ok")
