"""The model router's own wire models.

The router speaks the OpenAI **Chat Completions** shape — the de-facto standard
that LiteLLM and OpenRouter also use — so any client that can produce a
chat-completions request can call it, and it can fan out to any provider that
speaks the same shape.

The request is the router's own dataclass (below). The *response* is reused
verbatim from the OpenAI SDK's ``ChatCompletion`` (that is exactly what a
chat-completions call returns, so there is nothing to translate) — see
``service.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatCompletionRequest:
    """A model-invocation request in Chat Completions / LiteLLM / OpenRouter form.

    ``model`` selects the target (a future router keys its backend choice off
    this). ``messages`` and ``params`` are the standard chat-completions fields:
    ``params`` carries everything else the caller set — ``tools``,
    ``tool_choice``, ``temperature``, ``response_format``, ``max_tokens``,
    ``parallel_tool_calls``, … — already in OpenAI/LiteLLM shape, so the handler
    forwards them as ``create(**params)``.
    """

    model: str
    messages: list[dict[str, Any]]
    params: dict[str, Any] = field(default_factory=dict)
