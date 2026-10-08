"""Worker for the Modal coding agent.

Run from this example's directory with ``just worker``, or from the repo root with:
    uv run --group examples --with 'modal==1.6.1' python -m examples.modal_sandbox.worker

Hosts the ModalCodingAgent workflow. ``AgentHarnessPlugin`` registers the activities: the
sandbox activities for the ``modal`` provider (the sandbox tools run in the workflow on them)
and the example's own ``preview_url``. Run exactly one: the workspace snapshots are files on this worker's disk, and
processes the agent leaves running are reachable only through this worker.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    MODAL_TOKEN_ID / MODAL_TOKEN_SECRET       Modal credentials, unless ~/.modal.toml has them
    GEMINI_API_KEY                            required — the agent calls the Gemini API
    MODAL_SANDBOX_TASK_QUEUE                  task queue to poll (default: modal-sandbox)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from agents.extensions.sandbox.modal import ModalSandboxClient
from google.genai import Client as GeminiClient
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin
from temporal_agent_harness.harness.sandbox import SandboxClientProvider
from temporal_agent_harness.plugin import AgentHarnessPlugin

from .workflow import PROVIDER, TASK_QUEUE, ModalCodingAgentWorkflow, preview_url


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("MODAL_SANDBOX_TASK_QUEUE", TASK_QUEUE)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("error: GEMINI_API_KEY env var not set")

    # AgentHarnessPlugin last: it leaves the Gemini plugin's payload converter in place and adds
    # the harness's large-payload offload, matching the session-manager worker and web server.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[
            GoogleGenAIPlugin(GeminiClient(api_key=api_key)),
            AgentHarnessPlugin(
                tools=[preview_url],
                sandbox_clients=[SandboxClientProvider(PROVIDER, ModalSandboxClient())],
            ),
        ],
    )

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[ModalCodingAgentWorkflow],
        # The workflow module imports the OpenAI sandbox and Modal types pass-through.
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "agents", "openai", "modal"
            )
        ),
    )
    print(
        f"Modal coding agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
