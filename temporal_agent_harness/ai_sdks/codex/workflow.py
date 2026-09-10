"""Workflow-side Codex invocation: all CLI I/O runs in one durable activity."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
    from ._models import ACTIVITY_NAME, CodexRequest, CodexResult


async def run_codex(prompt: str, *, runner: AgentWorkflowRunner) -> CodexResult:
    """Run a fresh Codex session and stream its native tools into this agent turn.

    Callers provide conversation context explicitly in the prompt. Completed
    activity results replay without rerunning Codex. Failures are not retried:
    a coding process may have edited files before its result was recorded.
    """
    context = runner.current_stream_context
    if context is None:
        raise RuntimeError("run_codex must be called inside an active harness turn")
    request = CodexRequest(
        prompt=prompt, invocation_id=str(workflow.uuid4()), stream_context=context,
    )
    return await workflow.execute_activity(
        ACTIVITY_NAME, request, result_type=CodexResult,
        start_to_close_timeout=timedelta(hours=2),
        heartbeat_timeout=timedelta(seconds=30),
        retry_policy=RetryPolicy(maximum_attempts=1),
        summary="Codex inner agent loop",
    )
