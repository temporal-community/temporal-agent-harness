"""Worker for the OpenAI hosted-file_search doc-QA agent.

Run from the repo root with:
    uv run --group examples python -m examples.hello_openai_file_search.worker

(or `just worker` from examples/hello_openai_file_search.)

Hosts the HelloOpenAIFileSearchAgent workflow. Two jobs beyond the usual wiring:

1. **Resolve the vector store, then EXPORT it, before polling starts.** Creating/ingesting a
   vector store is ordinary OpenAI API work — network I/O with non-deterministic ids — so it
   happens here, once, at startup. The id is then exported as ``OPENAI_VECTOR_STORE_ID``, which the
   workflow module reads at import. It has to travel by environment rather than by assigning
   ``workflow.VECTOR_STORE_ID``: the Temporal workflow sandbox re-imports the workflow module into
   its own namespace, so an assignment made to the host module is invisible to the running
   workflow. Set ``OPENAI_VECTOR_STORE_ID`` yourself to reuse a store and skip creation.
2. **Wire the harness streaming seam**, exactly as the other OpenAI examples do
   (``stream_to_provider`` + ``observer_factory``), so a turn's model events reach the turn stream.

Note what is NOT wired: the hosted ``file_search`` tool needs nothing worker-side. The vendored
plugin already forwards that tool type into the model activity, which is the point this example is
making — OpenAI's retrieval is available to a harness agent with no harness changes at all.

Env vars (set in the repo-root .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required — model calls AND vector-store setup
    OPENAI_VECTOR_STORE_ID                    optional — reuse this store instead of creating one
    HELLO_OPENAI_FS_TASK_QUEUE                task queue (default: hello-openai-file-search)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import timedelta

from openai import OpenAI
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
)

from .corpus import VECTOR_STORE_NAME, ensure_vector_store
from .workflow import TASK_QUEUE, HelloOpenAIFileSearchAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("HELLO_OPENAI_FS_TASK_QUEUE", TASK_QUEUE)

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    # Resolve the corpus BEFORE the worker starts polling, so no turn can observe a half-set up
    # store. Idempotent by name, so restarts reuse it.
    store_id = os.environ.get("OPENAI_VECTOR_STORE_ID")
    if store_id:
        print(f"Using existing vector store {store_id}", flush=True)
    else:
        print(
            f"Resolving vector store {VECTOR_STORE_NAME!r} (creating + ingesting if new)…",
            flush=True,
        )
        store_id = ensure_vector_store(OpenAI())
        print(f"Vector store ready: {store_id}", flush=True)
    # Export it rather than assigning workflow_module.VECTOR_STORE_ID: the workflow sandbox
    # re-imports the workflow module into its own namespace, so an assignment to the host module
    # is invisible there, while the environment is process-global and IS visible to the re-import.
    # Must happen before the Worker starts polling.
    os.environ["OPENAI_VECTOR_STORE_ID"] = store_id

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            # file_search adds a server-side retrieval hop before the model answers, so give the
            # model activity more headroom than a plain chat turn needs.
            start_to_close_timeout=timedelta(minutes=3),
            heartbeat_timeout=timedelta(seconds=30),
            stream_to_provider=stream_to_provider,
        ),
        observer_factory=harness_observer_factory,
    )

    # The plugin supplies its own (OpenAI-aware, pydantic-compatible) data converter.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[plugin])

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[HelloOpenAIFileSearchAgentWorkflow],
        # No tool activities: get_weather is inline and file_search is hosted by OpenAI.
        activities=[],
    )
    print(
        f"Hello OpenAI file_search agent worker ready: "
        f"vector_store={store_id} "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
