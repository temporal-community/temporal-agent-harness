"""The router's own request model (Chat Completions / LiteLLM / OpenRouter shape).

The response type is reused verbatim from the OpenAI SDK's ``ChatCompletion`` —
see ``service.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatCompletionRequest:
    """A chat-completions request. ``model`` selects the target (a future router
    keys its backend off this); ``params`` carries the remaining OpenAI/LiteLLM
    ``create(**params)`` kwargs (``tools``, ``tool_choice``, ``temperature``, …)."""

    model: str
    messages: list[dict[str, Any]]
    params: dict[str, Any] = field(default_factory=dict)
