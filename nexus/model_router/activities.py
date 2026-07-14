"""The activity that actually calls a model provider.

Runs on the router worker (normal activity context — real I/O is fine). This is
where provider selection would live: today it always calls OpenAI's
chat-completions endpoint; a multi-provider router would pick a backend from
``request.model`` (e.g. via ``litellm.acompletion(...)``) here.

It lives in an activity (not inline in the Nexus operation) because model calls
are slow and unbounded — see ``workflow.py`` / ``handler.py`` for why the router
operation is workflow-backed rather than synchronous.
"""

from __future__ import annotations

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from temporalio import activity

from .models import ChatCompletionRequest


class ModelRouterActivities:
    """Holds the reusable model client. Defaults to OpenAI with retries disabled
    (so Temporal activity retries govern retry behavior)."""

    def __init__(self, client: AsyncOpenAI | None = None) -> None:
        self._client = client or AsyncOpenAI(max_retries=0)

    @activity.defn
    async def invoke_chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletion:
        # TODO(router): pick a backend from request.model instead of always OpenAI.
        return await self._client.chat.completions.create(
            model=request.model,
            messages=request.messages,
            **request.params,
        )
