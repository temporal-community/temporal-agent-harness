"""Worker for the hello-world Gemini agent — and the whole consumer-vs-GEAP switch.

Run from the repo root with:
    uv run --group examples python -m examples.hello_gemini_enterprise.worker

(or `just worker` from examples/hello_gemini_enterprise.)

Hosts only the HelloGeminiEnterpriseAgent workflow. Its one tool (``get_weather``) is an inline
harness tool with no worker-side body, so there are no tool activities to register — the only
thing registered here is the Gemini plugin, which is exactly what this example is testing.

**This file is the entire backend decision.** ``workflow.py`` never names a backend: it calls
``gemini.interactions.create(...)`` on the harness's Temporal-aware shim, which forwards the
kwargs into the plugin's activity, and the real ``genai.Client`` constructed below is what
resolves the endpoint and the credentials. So pointing this agent at Google's **Gemini
Enterprise Agent Platform** (GEAP — the 2026 rebrand of Vertex AI) instead of the consumer
Gemini Developer API is a change to ``_gemini_client`` and nothing else — no workflow edit, no
new workflow history.

.. warning::
    **Measured 2026-08-10: the GEAP branch does not work for THIS agent.** GEAP hosts the
    Interactions API but refuses the shape this agent needs:
    ``interactions.create(model=...)`` returns ``400 'Unsupported model interaction: <model>'``
    for every model id and name form, while ``models.generate_content`` on the very same client
    and model succeeds — and you cannot route around it via a custom Agent, because
    ``agents.create`` accepts only ``base_agent='antigravity-preview-05-2026'``. So the client
    swap below is necessary but NOT sufficient for an Interactions-API agent. It is still
    correct, and still the whole configuration story: what's missing is the API surface, not the
    wiring. Full evidence in README.md ("The verdict").

Env vars (set in the repo-root .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE  Temporal connection profile
    HELLO_GEAP_TASK_QUEUE                    task queue to poll (default: hello-gemini-enterprise)

    GOOGLE_GENAI_USE_ENTERPRISE  "true" to call GEAP / Agent Platform; anything else (or unset)
    (or GOOGLE_GENAI_USE_VERTEXAI) uses the consumer Gemini Developer API. Both names are read by
                               the google-genai SDK itself — ENTERPRISE is the current spelling,
                               VERTEXAI the legacy alias — so this is the SDK's idiom, not ours.
    When unset/false — consumer Gemini Developer API:
        GEMINI_API_KEY         required. Endpoint: generativelanguage.googleapis.com
    When true — GEAP / Agent Platform:
        GOOGLE_CLOUD_PROJECT   required — the project to authorize and bill against
        GOOGLE_CLOUD_LOCATION  region, or "global" (default). "global" resolves to
                               aiplatform.googleapis.com; a region resolves to
                               <region>-aiplatform.googleapis.com
        ...plus Application Default Credentials on THIS host — the worker is the only process
        that authenticates. Get them with `gcloud auth application-default login`, or run on a
        service account. The API key is unused in this mode.

    GEAP prerequisite: the Agent Platform API must be enabled on the project
    (`gcloud services enable aiplatform.googleapis.com --project <project>`). Without it the
    model activity fails with 403 SERVICE_DISABLED, naming the project and an activation URL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from google.genai import Client as GeminiClient
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin
from temporal_agent_harness.utils.large_payload import with_large_payload_offload

from .workflow import TASK_QUEUE, HelloGeminiEnterpriseAgentWorkflow

DEFAULT_LOCATION = "global"


def _use_geap() -> bool:
    """Whether to target GEAP / Agent Platform rather than the consumer Gemini API.

    Honors both spellings the google-genai SDK itself accepts —
    ``GOOGLE_GENAI_USE_ENTERPRISE`` (current) and ``GOOGLE_GENAI_USE_VERTEXAI`` (legacy) — so
    whichever one is already in an environment works here too. Either being truthy selects GEAP.
    """
    return any(
        os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}
        for name in ("GOOGLE_GENAI_USE_ENTERPRISE", "GOOGLE_GENAI_USE_VERTEXAI")
    )


def _gemini_client() -> GeminiClient:
    """Build the ONE real ``genai.Client`` this worker owns — the backend decision, in full.

    Both branches produce an ordinary ``genai.Client``; the plugin, the activities, and the
    workflow are identical either way. The two differences that matter:

    * **Auth.** The consumer API takes an API key. GEAP takes no key — it uses Application
      Default Credentials, and the SDK mints a bearer token per request inside the activity.
      Either way the credential never enters the workflow or Temporal's event history.
    * **Endpoint.** ``vertexai=True`` plus a project/location makes the SDK address
      ``…-aiplatform.googleapis.com`` and prefix every path with
      ``projects/<project>/locations/<location>/``.

    Constructing this eagerly (rather than lazily inside the activity) is deliberate: a
    misconfigured backend fails here, at worker startup, with a readable message — not on a
    user's first turn.
    """
    if not _use_geap():
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            sys.exit(
                "error: GEMINI_API_KEY env var not set.\n"
                "Set it for the consumer Gemini Developer API, or set "
                "GOOGLE_GENAI_USE_VERTEXAI=true (plus GOOGLE_CLOUD_PROJECT) to use GEAP."
            )
        return GeminiClient(api_key=api_key)

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        sys.exit(
            "error: GOOGLE_GENAI_USE_VERTEXAI is set but GOOGLE_CLOUD_PROJECT is not.\n"
            "GEAP authorizes and bills per project — set GOOGLE_CLOUD_PROJECT to the project "
            "that has the Agent Platform API enabled."
        )
    location = os.environ.get("GOOGLE_CLOUD_LOCATION") or DEFAULT_LOCATION
    # `enterprise=` is the current name for what used to be `vertexai=`; the SDK treats them as
    # exact aliases (`resolved_vertexai = enterprise if enterprise is not None else vertexai`,
    # google/genai/client.py) and its own docstring calls `vertexai` the legacy flag. Using the
    # new name to match Google's current GEAP samples — it changes nothing at runtime.
    return GeminiClient(enterprise=True, project=project, location=location)


def _backend_description() -> str:
    """One line naming the backend, printed at startup so a run is never ambiguous."""
    if not _use_geap():
        return "consumer Gemini Developer API (generativelanguage.googleapis.com)"
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION") or DEFAULT_LOCATION
    return f"GEAP / Agent Platform (project={project!r} location={location!r})"


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    for name in ("temporalio", "temporalio.workflow", "temporalio.activity"):
        logging.getLogger(name).setLevel(logging.INFO)

    task_queue = os.environ.get("HELLO_GEAP_TASK_QUEUE", TASK_QUEUE)

    plugin = GoogleGenAIPlugin(_gemini_client())

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[plugin],
        # Match the session-manager worker + server converter (large-payload offload) so every
        # process in the shared example stack reads the same payloads.
        data_converter=await with_large_payload_offload(pydantic_data_converter),
    )

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[HelloGeminiEnterpriseAgentWorkflow],
        # No tool activities: get_weather is an inline workflow tool. The Gemini interactions
        # activity is registered by the plugin above.
        activities=[],
    )
    print(
        f"Hello Gemini Enterprise agent worker ready: "
        f"backend={_backend_description()} "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
