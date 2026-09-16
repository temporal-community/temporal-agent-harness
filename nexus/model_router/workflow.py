"""The workflow that backs the router's Nexus operation.

Each ``chat_completion`` Nexus call starts one of these. It exists so the model
call can be **asynchronous and unbounded**: a Nexus *sync* operation must return
in ~10s (it resolves inline in the StartOperation RPC), which LLM calls routinely
exceed. A workflow-backed operation instead completes whenever this workflow
completes — durably, with the model call retried as an activity.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from openai.types.chat import ChatCompletion

    from .activities import ModelRouterActivities
    from .models import ChatCompletionRequest


@workflow.defn
class ModelRouterWorkflow:
    """Runs one model call as an activity and returns its response."""

    @workflow.run
    async def run(self, request: ChatCompletionRequest) -> ChatCompletion:
        return await workflow.execute_activity_method(
            ModelRouterActivities.invoke_chat_completion,
            request,
            # Per-attempt cap; schedule_to_close bounds the whole activity
            # INCLUDING retries, so the total stays below the caller's Nexus
            # operation timeout (nexus_transport._OP_TIMEOUT) — otherwise retries
            # would be cut off mid-flight when the operation times out.
            start_to_close_timeout=timedelta(minutes=2),
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
