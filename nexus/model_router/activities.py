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

import os
from datetime import timedelta
from typing import NoReturn

from openai import APIStatusError, AsyncOpenAI
from openai.types.chat import ChatCompletion
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .models import ChatCompletionRequest


def _raise_for_openai_status(e: APIStatusError) -> NoReturn:
    """Translate an OpenAI APIStatusError into the right Temporal retry posture.

    The client runs with ``max_retries=0`` (below), so retries are governed by
    the activity's retry policy — which retries every exception by default. That
    is wrong for permanent errors (400 bad request, 401 auth, most 4xx): they
    would be retried identically to no effect. Mark those non-retryable, honoring
    OpenAI's ``x-should-retry`` and ``retry-after`` hints. (Same classification
    the OpenAI Agents plugin applies on its activity path.)
    """
    retry_after: timedelta | None = None
    retry_after_ms = e.response.headers.get("retry-after-ms")
    if retry_after_ms is not None:
        retry_after = timedelta(milliseconds=float(retry_after_ms))
    elif (retry_after_s := e.response.headers.get("retry-after")) is not None:
        retry_after = timedelta(seconds=float(retry_after_s))

    should_retry = e.response.headers.get("x-should-retry")
    if should_retry == "true":
        raise e  # retryable per OpenAI; let the activity retry policy handle it
    if should_retry == "false":
        raise ApplicationError(
            "Non retryable OpenAI error",
            non_retryable=True,
            next_retry_delay=retry_after,
        ) from e

    # Retry 408 (timeout), 409 (conflict), 429 (rate limit), and any 5xx; every
    # other 4xx is a caller error that won't recover on retry.
    retryable = (
        e.response.status_code in (408, 409, 429) or e.response.status_code >= 500
    )
    raise ApplicationError(
        f"{'Retryable' if retryable else 'Non retryable'} OpenAI status "
        f"{e.response.status_code}",
        non_retryable=not retryable,
        next_retry_delay=retry_after,
    ) from e


class ModelRouterActivities:
    """Holds the reusable model client. Defaults to OpenAI with retries disabled
    (so Temporal activity retries govern retry behavior)."""

    def __init__(
        self, client: AsyncOpenAI | None = None, error_client: AsyncOpenAI | None = None
    ) -> None:
        self._client = client or AsyncOpenAI(max_retries=0)
        self._error_client = error_client or AsyncOpenAI(
            max_retries=0,
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            base_url="https://api.anthropic.com/v1/",
        )

    @activity.defn
    async def invoke_chat_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletion:
        # TODO(router): pick a backend from request.model instead of always OpenAI.
        try:
            return await self._client.chat.completions.create(
                model=request.model,
                messages=request.messages,
                **request.params,
            )
        except APIStatusError as e:
            _raise_for_openai_status(e)

    @activity.defn
    async def invoke_chat_completion_error(
        self, request: ChatCompletionRequest
    ) -> ChatCompletion:
        # TODO(router): pick a backend from request.model instead of always OpenAI.
        try:
            return await self._client.chat.completions.create(
                model=request.model,
                messages=request.messages,
                **request.params,
            )
        except APIStatusError as e:
            _raise_for_openai_status(e)
