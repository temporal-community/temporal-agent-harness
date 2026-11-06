"""Worker for the hello-world LangGraph agent.

Run from the repo root with:
    uv run --group examples python -m examples.langgraph_hello.worker

Hosts only the LangGraphHelloAgent workflow. Two plugins on the client, in this order:
  * ``LangGraphPlugin`` — the temporalio-langgraph plugin, vendored as ai_sdks.langgraph. It registers the
    graph's activity nodes and the workflow-sandbox passthroughs LangGraph needs.
  * ``AgentHarnessPlugin``, after it — the harness's data converter (large-payload offload) and
    activities. Last, so its converter wins.
``AgentHarnessPlugin(tools=...)`` registers the ``get_weather`` activity tool's body.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required — the agent calls the OpenAI API
    LANGGRAPH_HELLO_TASK_QUEUE                task queue to poll (default: langgraph-hello)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.langgraph import LangGraphPlugin
from temporal_agent_harness.plugin import AgentHarnessPlugin

from examples.langgraph_hello.workflow import (
    GRAPH,
    TASK_QUEUE,
    LangGraphHelloAgentWorkflow,
    build_graph,
    get_weather,
)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("LANGGRAPH_HELLO_TASK_QUEUE", TASK_QUEUE)

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[LangGraphPlugin(graphs={GRAPH: build_graph()}), AgentHarnessPlugin(tools=[get_weather])],
    )

    worker = Worker(client, task_queue=task_queue, workflows=[LangGraphHelloAgentWorkflow])
    print(
        f"LangGraph hello agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
