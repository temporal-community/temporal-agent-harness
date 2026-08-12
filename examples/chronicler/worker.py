"""Worker for the Chronicler example agent.

Run from the repo root with:
    uv run python -m examples.chronicler.worker

(or `just worker` from examples/chronicler, which installs the project's `examples` dependency
group first.)

Connection settings come from a ``temporal.toml`` profile, resolved through temporalio's
``ClientConfig.load_client_connect_config()``, which reads TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE
from the environment. The example sets these in ``.env.local`` (see examples/chronicler/README.md).

Env vars:
    TEMPORAL_CONFIG_FILE          path to a temporal.toml (set in .env.local)
    TEMPORAL_PROFILE              profile name to load (default: "default")
    GEMINI_API_KEY                required — recap drafting and TTS call Gemini
    CHRONICLER_AGENT_TASK_QUEUE   task queue to poll (default: chronicler-agent)

Hosts the conversational Chronicler parent and its model-free audio child — not the session
manager. The packaged session manager is hosted by
examples.chronicler.session_manager_worker; because it launches agents by registered name, it
dispatches this agent to this queue without this worker hosting the manager.
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

from .audio_activities import synthesize_approved_audio
from .audio_workflow import ChroniclerAudioWorkflow
from .conversational_workflow import TASK_QUEUE, ChroniclerAgentWorkflow


CHRONICLER_WORKFLOWS = (
    ChroniclerAgentWorkflow,
    ChroniclerAudioWorkflow,
)

CHRONICLER_ACTIVITIES = (synthesize_approved_audio,)


async def main() -> None:
    # INFO logging keeps synthesis and workflow progress visible.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    for name in ("temporalio", "temporalio.workflow", "temporalio.activity"):
        logging.getLogger(name).setLevel(logging.INFO)

    task_queue = os.environ.get("CHRONICLER_AGENT_TASK_QUEUE", TASK_QUEUE)

    # Source text and synthesized audio can exceed Temporal's payload limit. The offload codec
    # stores big payloads externally and keeps a reference; every reader must use the same codec.
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("error: GEMINI_API_KEY env var not set")
    plugin = GoogleGenAIPlugin(GeminiClient(api_key=api_key))

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[plugin],
        data_converter=await with_large_payload_offload(pydantic_data_converter),
    )

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=list(CHRONICLER_WORKFLOWS),
        # Gemini interactions are registered by the plugin; this is the model-free TTS activity.
        activities=list(CHRONICLER_ACTIVITIES),
    )
    print(
        f"Chronicler agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
