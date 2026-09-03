from temporalio import workflow
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from examples.nexus_hello.native_subagent import NativeResearchSubagentWorkflow


async def test_native_subagent_workflow_passes_worker_sandbox_validation() -> None:
    SandboxedWorkflowRunner().prepare_workflow(
        workflow._Definition.must_from_class(NativeResearchSubagentWorkflow)
    )
