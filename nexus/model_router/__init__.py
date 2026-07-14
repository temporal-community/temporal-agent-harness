"""A model router exposed over Temporal Nexus.

Speaks the OpenAI Chat Completions / LiteLLM / OpenRouter shape; today forwards
to OpenAI, later routes to any provider. Standalone: nothing here depends on the
OpenAI Agents plugin.

Import the light contract (``ModelRouterService`` / ``ChatCompletionRequest`` /
``NEXUS_ENDPOINT``) from callers — including in workflow context — to build a
Nexus client. The handler (``ModelRouterServiceHandler``) and worker are
server-side only; import them from their submodules to avoid pulling an OpenAI
client into a workflow sandbox.
"""

from __future__ import annotations

from .models import ChatCompletionRequest
from .service import NEXUS_ENDPOINT, TASK_QUEUE, ModelRouterService

__all__ = [
    "ChatCompletionRequest",
    "ModelRouterService",
    "NEXUS_ENDPOINT",
    "TASK_QUEUE",
]
