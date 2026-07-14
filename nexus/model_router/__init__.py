"""A model router exposed over Temporal Nexus.

Speaks the OpenAI Chat Completions / LiteLLM / OpenRouter shape; today forwards
to OpenAI, later routes to any provider. Standalone: nothing here depends on the
OpenAI Agents plugin.

Import from the submodules, not this package, on purpose: ``service`` (and thus
``handler``) pulls in the OpenAI SDK for the ``ChatCompletion`` wire type, and this
package's ``workflow`` module is loaded inside a Temporal workflow sandbox. Keeping
``__init__`` import-light means loading the workflow doesn't drag ``openai`` through
the sandbox unguarded. So:

* callers of the contract:  ``from nexus.model_router.service import ModelRouterService, NEXUS_ENDPOINT``
                            ``from nexus.model_router.models import ChatCompletionRequest``
* the worker:               ``from nexus.model_router.worker`` (registers everything)
"""
