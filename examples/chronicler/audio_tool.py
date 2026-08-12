"""Parent-owned adapter for the model-free Chronicler audio child."""

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy

from temporal_agent_harness.harness import agent

from .audio_models import (
    AudioApprovalPackage,
    AudioGenerationRequest,
    AudioGenerationResult,
)
from .audio_workflow import ChroniclerAudioWorkflow

AUDIO_CHILD_ID_PREFIX = "chronicler-audio--"


def audio_child_workflow_id(parent_workflow_id: str) -> str:
    """Derive the one fixed audio-child ID owned by a parent chat workflow."""
    return f"{AUDIO_CHILD_ID_PREFIX}{parent_workflow_id}"


async def launch_audio_child(
    request: AudioGenerationRequest,
) -> AudioGenerationResult:
    """Start and await the parent's single fixed audio child."""
    parent_info = workflow.info()
    return await workflow.execute_child_workflow(
        ChroniclerAudioWorkflow.run,
        request,
        id=audio_child_workflow_id(parent_info.workflow_id),
        task_queue=parent_info.task_queue,
        result_type=AudioGenerationResult,
        parent_close_policy=workflow.ParentClosePolicy.TERMINATE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )


async def _generate_audio_implementation(
    package: AudioApprovalPackage,
) -> AudioGenerationResult:
    """Generate the one freshly approved audio package in a model-free child."""
    return await launch_audio_child(AudioGenerationRequest(package=package, mode="normal"))


_generate_audio_implementation.__name__ = "generate_audio"
_generate_audio_implementation.__qualname__ = "generate_audio"
generate_audio = agent.tool_defn(always_require_approval=True)(
    _generate_audio_implementation
)
