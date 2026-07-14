"""Bridge: run the agent's model over the model-router Nexus service.

This is the whole "transport swap". The OpenAI Agents SDK's
``OpenAIChatCompletionsModel`` already turns a model call into a chat-completions
request and turns the response back into a ``ModelResponse`` — the only thing it
does over the network is ``client.chat.completions.create(...)``. So we hand it a
stand-in client whose ``create`` goes over Nexus to ``ModelRouterService`` instead
of HTTP to OpenAI.

``nexus_model_provider`` is wired onto the plugin as
``ModelActivityParameters.workflow_model_provider``. The stub calls it in workflow
context, so the ``create_nexus_client`` call inside ``NexusChatCompletions.create``
is valid. No serialization, no tool reconstruction, no bespoke translation: the
SDK produces/consumes the (LiteLLM/OpenRouter-shaped) chat-completions payload,
and the router owns the wire types.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from openai import NotGiven, Omit

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from agents import Model, OpenAIChatCompletionsModel
    from openai.types.chat import ChatCompletion

    from nexus.model_router import (
        NEXUS_ENDPOINT,
        ChatCompletionRequest,
        ModelRouterService,
    )

# Bounds the Nexus operation (a single model call).
_OP_TIMEOUT = timedelta(minutes=2)

# Keys the OpenAI SDK sets on the create call that are client/transport-only or
# streaming — they don't belong on the router wire.
_DROP_KEYS = frozenset(
    {"extra_headers", "extra_query", "extra_body", "stream", "stream_options"}
)


def _is_unset(value: Any) -> bool:
    # The OpenAI SDK fills unspecified params with `omit` / NOT_GIVEN sentinels.
    return value is None or isinstance(value, (Omit, NotGiven))


class NexusChatCompletions:
    """``client.chat.completions`` stand-in: one async ``create`` over Nexus."""

    async def create(self, **kwargs: Any) -> ChatCompletion:
        clean = {
            k: v
            for k, v in kwargs.items()
            if k not in _DROP_KEYS and not _is_unset(v)
        }
        model = clean.pop("model")
        messages = clean.pop("messages")
        request = ChatCompletionRequest(model=model, messages=messages, params=clean)

        nexus_client = workflow.create_nexus_client(
            service=ModelRouterService, endpoint=NEXUS_ENDPOINT
        )
        return await nexus_client.execute_operation(
            ModelRouterService.chat_completion,
            request,
            schedule_to_close_timeout=_OP_TIMEOUT,
        )


class NexusChatClient:
    """Minimal ``AsyncOpenAI`` stand-in for ``OpenAIChatCompletionsModel``.

    The SDK only calls ``.chat.completions.create(...)`` and reads ``.base_url``
    (to detect the official OpenAI endpoint — a ``nexus://`` URL reads as "not
    official", which is what we want). Nothing else is touched.
    """

    base_url = "nexus://model-router"

    def __init__(self) -> None:
        self.chat = _NexusChat()


class _NexusChat:
    def __init__(self) -> None:
        self.completions = NexusChatCompletions()


def nexus_model_provider(model_name: str | None) -> Model:
    """Resolve a workflow-side Model whose transport is the router Nexus service."""
    return OpenAIChatCompletionsModel(
        model=model_name or "gpt-4.1-mini",
        openai_client=NexusChatClient(),  # type: ignore[arg-type]
    )
