"""Worker for the Playpen support agent.

Run from the repo root with:
    uv run python -m examples.playpen.worker

(or `just worker` from examples/playpen).

Env vars:
    TEMPORAL_CONFIG_FILE      path to a temporal.toml (set in .env.local)
    TEMPORAL_PROFILE          profile name to load (default: "default")
    GEMINI_API_KEY            required — the agent converses via the Gemini Interactions API
    DOGWOOD_CLI               required — path to the `dogwood` CLI that decides each host call
                              (`just install-dogwood` builds one into examples/playpen/.dogwood)
    PLAYPEN_POLICY_DIR        the policy store (default: examples/playpen/policies)
    PLAYPEN_AGENT_TASK_QUEUE  task queue to poll (default: playpen-support-agent)

This worker hosts only the support agent and its policy engine. The session manager is hosted
by `temporal-agent-harness session-manager`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from google.genai import Client as GeminiClient
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin
from temporal_agent_harness.playpen.activity import DogwoodPolicyStore
from temporal_agent_harness.plugin import AgentHarnessPlugin

from . import store
from .workflow import TASK_QUEUE, PlaypenSupportAgentWorkflow

DEFAULT_POLICY_DIR = Path(__file__).parent / "policies"


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    for name in ("temporalio", "temporalio.workflow", "temporalio.activity"):
        logging.getLogger(name).setLevel(logging.INFO)

    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        sys.exit("error: GEMINI_API_KEY env var not set")
    dogwood_cli = os.environ.get("DOGWOOD_CLI")
    if not dogwood_cli:
        sys.exit(
            "error: DOGWOOD_CLI env var not set. Build the CLI with `just install-dogwood` "
            "(from examples/playpen); the justfile sets DOGWOOD_CLI to it."
        )
    policy_dir = Path(os.environ.get("PLAYPEN_POLICY_DIR", DEFAULT_POLICY_DIR))
    policy_store = DogwoodPolicyStore(policy_dir, dogwood_cli=dogwood_cli)
    task_queue = os.environ.get("PLAYPEN_AGENT_TASK_QUEUE", TASK_QUEUE)

    # Harness plugin LAST so the Gemini plugin's payload converter wins. The harness plugin
    # registers the store tools' activity bodies and the Code Mode sandbox activities; the
    # policy store's `decide` is the one activity added by hand.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[
            GoogleGenAIPlugin(GeminiClient(api_key=gemini_api_key)),
            AgentHarnessPlugin(tools=store.STORE_TOOLS),
        ],
    )
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[PlaypenSupportAgentWorkflow],
        activities=[policy_store.decide],
    )
    print(
        f"Playpen support agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue} policies={policy_dir} dogwood={dogwood_cli}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
