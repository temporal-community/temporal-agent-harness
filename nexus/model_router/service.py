"""The model router Nexus service contract.

One operation, ``chat_completion``, takes a :class:`ChatCompletionRequest` and
returns an OpenAI-SDK :class:`~openai.types.chat.ChatCompletion`. This is the LLM
API, exposed over Nexus rather than HTTP. Sync-vs-async is a handler concern
(see ``handler.py`` / ``workflow.py``); the contract is just in→out.

Constructs nothing, but importing it does pull in the OpenAI SDK (for the
``ChatCompletion`` type), so workflow-context callers import it under
``workflow.unsafe.imports_passed_through()`` (see the example's
``nexus_transport.py``). The handler lives in ``handler.py``; the worker in
``worker.py``.
"""

from __future__ import annotations

import nexusrpc
from openai.types.chat import ChatCompletion

from .models import ChatCompletionRequest

# The Nexus endpoint name + the task queue the router worker polls. Shared with
# callers so they can address the endpoint and with the worker so it registers
# (and creates) the matching endpoint.
NEXUS_ENDPOINT = "model-router-endpoint"
TASK_QUEUE = "model-router"


@nexusrpc.service
class ModelRouterService:
    """A model router exposed as a Nexus service: request in, model response out."""

    chat_completion: nexusrpc.Operation[ChatCompletionRequest, ChatCompletion]
