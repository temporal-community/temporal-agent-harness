"""Workflow utilities for Google Gemini SDK integration with Temporal.

This module provides utilities for using the Google Gemini SDK within Temporal
workflows.  The key entry point is:

- :func:`google_genai_client` — returns an ``AsyncClient`` backed by a
  ``TemporalApiClient`` that routes all API calls through Temporal activities.

Tools are defined with :func:`harness.agent.activity_tool_defn` (durable, activity-backed)
or :func:`harness.agent.tool_defn` (inline), and their model-facing schema is built with
:func:`function_param`. The in-workflow dispatch + ``AgentToolContext`` plumbing lives
inside those decorators (in the harness), not here.
"""

from __future__ import annotations

from google.genai.client import AsyncClient

from ._temporal_api_client import (
    TemporalApiClient,
)
from ._temporal_async_client import (
    TemporalAsyncClient,
)
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


def google_genai_client(
    *,
    vertexai: bool = False,
    project: str | None = None,
    location: str | None = None,
    activity_config: ActivityConfig | None = None,
    runner: AgentWorkflowRunner | None = None,
) -> AsyncClient:
    """Create a Gemini ``AsyncClient`` that routes API calls through Temporal activities.

    .. warning::
        This API is experimental and may change in future versions.
        Use with caution in production environments.

    Returns an ``AsyncClient`` backed by a :class:`TemporalApiClient`.  The
    SDK's code (including the AFC loop) runs in the workflow; only the actual
    HTTP API calls cross into activities.  Credentials are never fetched or
    stored in the workflow — the activity worker handles authentication
    independently.

    Call this from within a workflow ``run`` method:

    .. code-block:: python

        @workflow.defn
        class MyWorkflow:
            @workflow.run
            async def run(self, query: str) -> str:
                client = google_genai_client()
                response = await client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=query,
                    config=GenerateContentConfig(
                        tools=[my_tool],  # an @agent.activity_tool_defn object
                    ),
                )
                return response.text

    Args:
        vertexai: Whether to use Vertex AI API endpoints.  Must match the
            ``GoogleGenAIPlugin`` configuration on the worker side.  Defaults to
            ``False`` (Gemini Developer API).
        project: Google Cloud project ID.  Only needed when ``vertexai=True``
            and the SDK's request formatting requires it (e.g., cache
            operations).
        location: Google Cloud location.  Same conditions as ``project``.
        activity_config: Override the default activity configuration
            (timeouts, retry policy, etc.) for Gemini API call activities.
        runner: Optional. The workflow's
            :class:`harness.agent_workflow.AgentWorkflowRunner`. When
            provided, streamed ``generate_content`` calls publish
            ``reply_delta`` events to the workflow's ``WorkflowStream``
            from inside the streaming activity, tagged with the runner's
            current turn id. Without it, streaming still works — chunks
            just don't surface as fine-grained UI deltas.

    Returns:
        A ``google.genai.client.AsyncClient`` instance.
    """
    temporal_api_client = TemporalApiClient(
        vertexai=vertexai,
        project=project,
        location=location,
        activity_config=activity_config,
        runner=runner,
    )
    return TemporalAsyncClient(temporal_api_client, activity_config)
