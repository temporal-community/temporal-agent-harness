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
    GEMINI_API_KEY                required — transcription, summarization, and TTS call Gemini
    CHRONICLER_AGENT_TASK_QUEUE   task queue to poll (default: chronicler-agent)
    CHRONICLER_NOTIFIER           notification channel: "inapp" (default) or "webhook"
    CHRONICLER_WEBHOOK_URL        webhook URL when CHRONICLER_NOTIFIER=webhook

Hosts the ChroniclerAgent (conversational, Code Mode over the durable audio tools) — not the
session manager. The packaged session manager is hosted by
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
from temporal_agent_harness.harness.code_mode.activities import CODE_MODE_ACTIVITIES
from temporal_agent_harness.harness.subagent_activities import SubagentActivities
from temporal_agent_harness.utils.large_payload import with_large_payload_offload

from . import chronicler_activities as tools
from .conversational_subagent_workflow import ChroniclerSubagentWorkflow
from .conversational_workflow import TASK_QUEUE, ChroniclerAgentWorkflow
from .scribe_workflow import ChroniclerScribeAgentWorkflow


async def main() -> None:
    # INFO logging so the per-tool trace (transcribe → summarize → synthesize → notify) is
    # visible; force=True so our config wins over any handler an import already installed.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    for name in ("temporalio", "temporalio.workflow", "temporalio.activity"):
        logging.getLogger(name).setLevel(logging.INFO)

    task_queue = os.environ.get("CHRONICLER_AGENT_TASK_QUEUE", TASK_QUEUE)

    # Transcripts and synthesized audio cross the activity boundary and land in workflow history,
    # so they can exceed Temporal's payload limit; the offload codec stores big payloads
    # externally and keeps a reference. Every process reading these payloads MUST use the same
    # codec, so the converters match across worker / session-manager / server.
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

    # Drives child SessionScribe subagents for the map-reduce conductor: run_subagent_turn sends
    # a turn to a child workflow and streams its reply back, so it closes over this client.
    subagents = SubagentActivities(client)
    worker = Worker(
        client,
        task_queue=task_queue,
        # Three agents share this worker/queue: the inline Code-Mode Chronicler, the map-reduce
        # conductor, and the SessionScribe child it drives as a subagent.
        workflows=[
            ChroniclerAgentWorkflow,
            ChroniclerSubagentWorkflow,
            ChroniclerScribeAgentWorkflow,
        ],
        # The durable audio tools (host functions dispatched by Code Mode or by a Scribe) plus the
        # Code Mode sandbox-stepping activities plus the subagent-turn activity (drives Scribes).
        # The Gemini interactions activity is registered by the plugin above.
        activities=[
            *tools.ALL_ACTIVITIES,
            *CODE_MODE_ACTIVITIES,
            subagents.run_subagent_turn,
        ],
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
