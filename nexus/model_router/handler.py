"""The model router Nexus handler.

Runs in a normal worker (no workflow sandbox), so it can do real I/O freely. It
receives a chat-completions request and returns the model's response.

Today it routes every request straight to OpenAI's chat-completions endpoint.
This is exactly the seam where a real router would select a backend from
``request.model`` and fan out to many providers — e.g. by calling
``litellm.acompletion(...)`` here instead of the OpenAI client. Because this runs
server-side (not in a workflow), a heavy multi-provider library like LiteLLM
belongs here, not on the caller.
"""

from __future__ import annotations

import nexusrpc.handler
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from .models import ChatCompletionRequest
from .service import ModelRouterService


@nexusrpc.handler.service_handler(service=ModelRouterService)
class ModelRouterServiceHandler:
    """Routes chat-completion requests to a provider. Today: always OpenAI.

    Defaults to an ``AsyncOpenAI`` client with retries disabled (so Temporal /
    Nexus retries govern retry behavior); pass a client to point elsewhere.
    """

    def __init__(self, client: AsyncOpenAI | None = None) -> None:
        self._client = client or AsyncOpenAI(max_retries=0)

    @nexusrpc.handler.sync_operation
    async def chat_completion(
        self,
        ctx: nexusrpc.handler.StartOperationContext,  # noqa: ARG002
        request: ChatCompletionRequest,
    ) -> ChatCompletion:
        # TODO(router): pick a backend from request.model instead of always OpenAI.
        # A multi-provider router would call litellm.acompletion(...) here.
        return await self._client.chat.completions.create(
            model=request.model,
            messages=request.messages,
            **request.params,
        )
