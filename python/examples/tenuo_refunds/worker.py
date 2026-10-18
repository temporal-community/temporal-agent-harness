"""Worker for the Tenuo refunds agent.

Run with `just worker`, or from the repo root with uv:
    PYTHONPATH=python uv run --project python --group examples python -m examples.tenuo_refunds.worker

Hosts the TenuoRefundsAgent workflow, its two activity tools, and the activity the Tenuo
evaluator uses to get a gated refund approved. Before it starts it builds the Tenuo config
(``keys.load()``); the workflow builds its evaluator from that config and the session's warrant.
Refunds the warrant gates are put to the support lead in this terminal.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required — the agent calls the OpenAI API
    TENUO_REFUNDS_TASK_QUEUE                  task queue to poll (default: tenuo-refunds)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from datetime import timedelta

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
)
from temporal_agent_harness.ai_sdks.tenuo import TenuoApprovalActivities
from temporal_agent_harness.plugin import AgentHarnessPlugin

from examples.tenuo_refunds import keys
from examples.tenuo_refunds.tools import ALL_TOOLS
from examples.tenuo_refunds.workflow import TASK_QUEUE, TenuoRefundsAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("TENUO_REFUNDS_TASK_QUEUE", TASK_QUEUE)

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")
    tenuo_config = keys.load()

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            stream_to_provider=stream_to_provider,
        ),
        observer_factory=harness_observer_factory,
    )

    # Harness plugin LAST, as in the other OpenAI examples. tools= registers each activity
    # tool's durable body.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config, plugins=[plugin, AgentHarnessPlugin(tools=ALL_TOOLS)]
    )

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[TenuoRefundsAgentWorkflow],
        # Gets the support lead's signed approval for refunds the warrant gates, through the
        # config's approval handler.
        activities=TenuoApprovalActivities(tenuo_config).all(),
        # The sandbox re-imports workflow code per run. Pass keys.py through so the workflow
        # sees the config keys.load() just built, rather than a fresh, empty copy of the module.
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules(
                "examples.tenuo_refunds.keys"
            )
        ),
    )
    print(
        f"Tenuo refunds agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    # Ctrl-C is how you stop it; the worker shuts down cleanly, so don't print a traceback.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
