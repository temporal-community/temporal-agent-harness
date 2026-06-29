"""Worker for the Monty example agents.

Run from the repo root with:
    uv run python -m examples.monty.worker

(or `just worker` from examples/monty, which installs the project's `examples` dependency
group first.)

Connection settings come from a ``temporal.toml`` profile, resolved through temporalio's
``ClientConfig.load_client_connect_config()``, which reads TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE
from the environment. The example sets these in ``.env.local`` (see examples/monty/README.md).

Env vars:
    TEMPORAL_CONFIG_FILE         path to a temporal.toml (set in .env.local)
    TEMPORAL_PROFILE             profile name to load (default: "default")
    OPENAI_API_KEY               required — the OpenAI conversational agents call OpenAI
    GEMINI_API_KEY               required — the Gemini conversational agents call Gemini
    MONTY_AGENT_TASK_QUEUE       task queue to poll (default: monty-dynamic-agent)

This worker hosts the Monty agents (dynamic + OpenAI/Gemini conversational variants) —
not the session manager. The packaged session manager is hosted by
examples.monty.session_manager_worker; because it launches agents by registered name,
it dispatches these agents to this queue without this worker hosting it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import timedelta

from google.genai import Client as GeminiClient
from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin
from temporal_agent_harness.ai_sdks.openai_agents_plugin import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
    OpenAIPayloadConverter,
)
from temporal_agent_harness.utils.large_payload import with_large_payload_offload
from temporalio.client import Client
from temporalio.converter import DataConverter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.harness.subagent_activities import SubagentActivities

from . import activities
from .conversational_gemini_subagent_workflow import MontyChatGeminiSubagentWorkflow
from .conversational_gemini_workflow import MontyChatGeminiAgentWorkflow
from .conversational_subagent_workflow import MontyChatOpenAISubagentWorkflow
from .conversational_workflow import MontyChatOpenAIAgentWorkflow
from .monty_activities import monty_resume_batch, monty_start_batch
from .workflow import TASK_QUEUE, MontyDynamicAgentWorkflow


async def main() -> None:
    # Loud logging so the workflow's per-step monty trace (start → host call → resume) is
    # visible (root logger defaults to WARNING otherwise). The workflow/activity logs go
    # to the "temporalio.*" loggers, which propagate to root — pin them to INFO explicitly.
    # force=True so our config wins even if an imported module already installed a root
    # handler — otherwise basicConfig is a silent no-op and no workflow logs would surface.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    for name in ("temporalio", "temporalio.workflow", "temporalio.activity"):
        logging.getLogger(name).setLevel(logging.INFO)

    task_queue = os.environ.get("MONTY_AGENT_TASK_QUEUE", TASK_QUEUE)

    # Match the session-manager worker + server: the large-payload offload codec. Monty
    # snapshot bytes cross the activity boundary and land in workflow history, so they can
    # exceed Temporal's payload limit; the codec offloads big payloads to external storage
    # and stores a reference. Every process that reads these payloads uses the same codec, so
    # the converters MUST match or offloaded payloads can't be read back.
    # The conversational Monty agents intentionally demonstrate two provider integrations
    # side by side. The original script-only MontyDynamicAgent doesn't use either plugin,
    # but sharing one worker keeps the demo simple. Register OpenAI first: its plugin
    # requires OpenAIPayloadConverter, which can still serialize the Pydantic payloads used
    # by the Gemini activity models.
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", openai_api_key),
            ("GEMINI_API_KEY", gemini_api_key),
        )
        if not value
    ]
    if missing:
        sys.exit(f"error: missing required env var(s): {', '.join(missing)}")
    plugins = [
        OpenAIAgentsPlugin(
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(minutes=3),
            ),
        ),
        GoogleGenAIPlugin(GeminiClient(api_key=gemini_api_key)),
    ]

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=plugins,
        data_converter=await with_large_payload_offload(
            DataConverter(payload_converter_class=OpenAIPayloadConverter)
        ),
    )

    # Monty agents run here: the script-only MontyDynamicAgent, inline OpenAI and Gemini
    # conversational agents, and both provider-specific subagent variants (which drive
    # MontyDynamicAgent as a subagent — so the child runs on this same queue). The session
    # manager is hosted by its own worker, not here; it dispatches these agents to this
    # queue by name.
    #
    # SubagentActivities closes over this worker's client so its run_subagent_turn activity can
    # send updates to + stream the reply from the child MontyDynamicAgent workflow. It's the
    # activity the subagent toolset's monty_run_script tool dispatches each turn.
    subagents = SubagentActivities(client)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[
            MontyDynamicAgentWorkflow,
            MontyChatOpenAIAgentWorkflow,
            MontyChatGeminiAgentWorkflow,
            MontyChatOpenAISubagentWorkflow,
            MontyChatGeminiSubagentWorkflow,
        ],
        # The travel-booking activities (the host functions) plus the Monty-stepping
        # activities (monty_start_batch / monty_resume_batch — the single async/concurrent
        # batch driver used by every Monty agent) plus the subagent-turn activity (drives the
        # script-runner child for both subagent workflows). The OpenAI and Gemini model
        # activities are registered by the plugins above.
        activities=[
            *activities.ALL_ACTIVITIES,
            monty_start_batch,
            monty_resume_batch,
            subagents.run_subagent_turn,
        ],
    )
    print(
        f"Monty dynamic agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
