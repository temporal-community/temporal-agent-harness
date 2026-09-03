"""The model router Nexus handler.

``chat_completion`` is an **asynchronous, workflow-backed** operation: it starts a
:class:`ModelRouterWorkflow` and returns its handle, so the operation completes
when that workflow does. See ``workflow.py`` for why it's workflow-backed rather
than a (time-capped) sync operation.
"""

from __future__ import annotations

import uuid

import nexusrpc.handler
from openai.types.chat import ChatCompletion

from temporalio import nexus

from .models import ChatCompletionRequest
from .service import ModelRouterService
from .workflow import ModelRouterWorkflow


@nexusrpc.handler.service_handler(service=ModelRouterService)
class ModelRouterServiceHandler:
    """Serves ``chat_completion`` by starting a router workflow per call."""

    @nexus.workflow_run_operation
    async def chat_completion(
        self,
        ctx: nexus.WorkflowRunOperationContext,
        request: ChatCompletionRequest,
    ) -> nexus.WorkflowHandle[ChatCompletion]:
        # The workflow runs on this handler worker's task queue by default — the
        # same worker that registers ModelRouterWorkflow (see worker.py).
        return await ctx.start_workflow(
            ModelRouterWorkflow.run,
            request,
            id=f"model-router-{uuid.uuid4()}",
        )
