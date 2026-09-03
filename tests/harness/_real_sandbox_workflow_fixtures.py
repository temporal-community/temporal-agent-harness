# ABOUTME: A sandboxed-tool agent whose workflow module wraps EVERY temporal_agent_harness import
# in one `imports_passed_through()` block — kept separate so Temporal's real SandboxedWorkflowRunner
# loads it fresh. See test_sandboxed_tool_works_under_real_sandboxed_workflow_runner.

from pathlib import Path

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel

    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent_protocol import AgentConfig, TextMessage, TextReply
    from temporal_agent_harness.harness.sandbox import SandboxConfig


class RealSandboxInput(BaseModel):
    pass


class RealSandboxResult(BaseModel):
    ok: bool = True


@agent.activity_tool_defn(sandboxed=True)
async def real_sandbox_probe(arg: RealSandboxInput) -> RealSandboxResult:
    return RealSandboxResult()


SANDBOX = SandboxConfig(backend="local", local_project_root=Path(__file__).parent)


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
        """Run the sandboxed tool once under the real workflow sandbox runner."""
        await self._runner.run_tool("call-1", real_sandbox_probe, RealSandboxInput())
        return TextReply(text="ok")
