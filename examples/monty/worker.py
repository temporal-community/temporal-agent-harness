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
    GEMINI_API_KEY               required — the conversational agents call the Gemini Interactions API
    MONTY_AGENT_TASK_QUEUE       task queue to poll (default: monty-dynamic-agent)

This worker hosts the three Monty agents (MontyDynamicAgent + the two conversational
agents) — not the session manager. The packaged session manager is hosted by the shared
examples.session_manager_worker; because it launches agents by registered name,
it dispatches these agents to this queue without this worker hosting it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from google.genai import Client as GeminiClient
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin

from . import activities
from .conversational_subagent_workflow import MontyChatSubagentWorkflow
from .conversational_workflow import MontyChatAgentWorkflow
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

    # The conversational Monty agent (MontyChatAgent) drives the Gemini Interactions API, so
    # this worker needs the Gemini plugin (it auto-registers the interactions activity). The
    # original script-only MontyDynamicAgent doesn't use it, but sharing one worker keeps the
    # demo simple.
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("error: GEMINI_API_KEY env var not set")

    # Two plugins, harness LAST so the Gemini plugin's payload converter wins:
    #   * GoogleGenAIPlugin  — the Gemini interactions activity.
    #   * AgentHarnessPlugin — everything the harness itself needs on this worker: the
    #     large-payload offload converter (Monty snapshot bytes cross the activity boundary
    #     and land in workflow history, so they can exceed Temporal's payload limit — and
    #     every process reading them must use the SAME converter, which is exactly what
    #     adding this plugin everywhere guarantees), the Code Mode sandbox-stepping
    #     activities (all three agents run their scripts through Code Mode), the
    #     subagent-turn activity (drives the script-runner child for MontyChatSubagentAgent),
    #     and the durable activity body of every travel tool in ALL_TOOLS.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[
            GoogleGenAIPlugin(GeminiClient(api_key=api_key)),
            AgentHarnessPlugin(tools=activities.ALL_TOOLS),
        ],
    )

    # All three Monty agents run here: the script-only MontyDynamicAgent, the inline
    # conversational MontyChatAgent, and the subagent-driven MontyChatSubagentAgent (which
    # drives MontyDynamicAgent as a subagent — so the child runs on this same queue). The
    # session manager is hosted by its own worker, not here; it dispatches these agents
    # to this queue by name.
    #
    # No `activities=` at all: a client plugin is applied to the workers built from that
    # client, so both plugins register their own activities here.
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[
            MontyDynamicAgentWorkflow,
            MontyChatAgentWorkflow,
            MontyChatSubagentWorkflow,
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
