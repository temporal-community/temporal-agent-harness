# ABOUTME: Hosts the LangGraph scripted agent until SIGTERM, for CONFORMANCE_WORKER:
# ABOUTME:   langgraph_worker --address HOST:PORT --namespace NS --task-queue QUEUE

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.ai_sdks.langgraph import LangGraphPlugin
from temporal_agent_harness.plugin import AgentHarnessPlugin
from tests.conformance.langgraph_scripted_agent import (
    ACTIVITY_TOOLS,
    GRAPH,
    LangGraphScriptedAgent,
    build_graph,
)


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--task-queue", required=True)
    args = parser.parse_args()
    client = await Client.connect(
        args.address,
        namespace=args.namespace,
        plugins=[LangGraphPlugin(graphs={GRAPH: build_graph()}), AgentHarnessPlugin(tools=ACTIVITY_TOOLS)],
    )
    worker = Worker(
        client,
        task_queue=args.task_queue,
        workflows=[LangGraphScriptedAgent],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    async with worker:
        await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
