"""First-class Temporal integration for the Google Gemini SDK.

.. warning::
    This module is experimental and may change in future versions.
    Use with caution in production environments.

This integration lets you use the Gemini SDK's async client with full
automatic function calling (AFC) support, plus a Temporal-aware shim of
the Interactions API. Every API call becomes a **durable Temporal
activity**. Define tools with :func:`harness.agent.tool_defn` (inline,
deterministic in-workflow) or :func:`harness.agent.activity_tool_defn`
(durable, activity-backed); for the latter, :func:`harness.agent.tool_activity`
returns the activity to register on the worker.

No credentials are fetched in the workflow, and no auth material appears in
Temporal's event history.

- :class:`GoogleGenAIPlugin` — registers all of the Gemini activities
  using a caller-provided ``genai.Client`` on the worker side.
- :func:`google_genai_client` — call from a workflow to get an
  ``AsyncClient`` whose ``models.generate_content*``, ``files``,
  ``file_search_stores``, and ``interactions`` modules all route through
  Temporal activities.
- :func:`function_param` — derive an Interactions-API ``FunctionParam``
  tool declaration from a tool's model-facing signature. The Interactions
  API has no AFC, so the workflow drives the tool-calling loop itself;
  pair this helper with the ``gemini.interactions.create(...)`` call on
  the client returned by :func:`google_genai_client`.

Quickstart::

    # ---- worker setup (outside the Temporal Python Sandbox) ----
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    plugin = GoogleGenAIPlugin(client)

    @agent.activity_tool_defn()
    async def get_weather(state: str) -> str: ...

    Worker(..., activities=[agent.tool_activity(get_weather), ...])

    # ---- workflow (inside the Temporal Python Sandbox) ----
    @workflow.defn
    class AgentWorkflow:
        @workflow.run
        async def run(self, query: str) -> str:
            client = google_genai_client()
            response = await client.models.generate_content(
                model="gemini-2.5-flash",
                contents=query,
                config=types.GenerateContentConfig(tools=[get_weather]),
            )
            return response.text
"""

from __future__ import annotations

from ._google_genai_plugin import GoogleGenAIPlugin
from ._interactions_workflow import function_param
from .workflow import (
    google_genai_client,
)

__all__ = [
    "GoogleGenAIPlugin",
    "function_param",
    "google_genai_client",
]
