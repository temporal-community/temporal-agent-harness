"""Worker for the governed analytics agent.

Run with `just worker` from examples/governed_analytics (Bazel runs it from the repo root).

Connection settings come from a ``temporal.toml`` profile (TEMPORAL_CONFIG_FILE /
TEMPORAL_PROFILE, set in the repo-root .env.local), as for the other examples.

Env vars:
    TEMPORAL_CONFIG_FILE          path to a temporal.toml (set in .env.local)
    TEMPORAL_PROFILE              profile name to load (default: "default")
    GEMINI_API_KEY                required: the model is Gemini
    GOVERNED_ANALYTICS_DATA_DIR   where the .duckdb files are (default: the example's data/,
                                  written by `just data`)

The tool policy itself is a separate service, the Dogwood verifier behind the `tool-verifier`
Nexus endpoint (`just endpoint`, `just verifier`). Without it every gated call fails its
check and goes to you for approval instead.
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

from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin
from temporal_agent_harness.plugin import AgentHarnessPlugin

from examples.governed_analytics.databases import DATABASES, data_dir
from examples.governed_analytics.sql_analysis import analyze_sql
from examples.governed_analytics.tools import ALL_TOOLS
from examples.governed_analytics.workflow import TASK_QUEUE, GovernedAnalyticsAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        sys.exit("error: GEMINI_API_KEY env var not set")
    missing = [d for d in DATABASES if not (data_dir() / f"{d}.duckdb").exists()]
    if missing:
        sys.exit(f"error: no {', '.join(missing)} database in {data_dir()}; run `just data`")

    # Harness last, so the Gemini plugin's payload converter wins. The harness plugin registers
    # the durable body of every data tool; analyze_sql is the policy call builder's activity.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[
            GoogleGenAIPlugin(GeminiClient(api_key=gemini_api_key)),
            AgentHarnessPlugin(tools=ALL_TOOLS),
        ],
    )
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[GovernedAnalyticsAgentWorkflow],
        activities=[analyze_sql],
    )
    print(
        f"Governed analytics agent worker ready: "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} taskQueue={TASK_QUEUE} "
        f"data={data_dir()}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
