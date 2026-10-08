"""Run the parent and persistent subagent on the harness; no provider keys needed."""

import asyncio
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents.testing import TestModelProvider
from temporal_agent_harness.plugin import AgentHarnessPlugin

from .models import LocalOpenAIModel
from .workflow import TASK_QUEUE, ParentAgent, ResearchAgent

NAMESPACE = "shared-tools-demo"


async def main() -> None:
    client = await Client.connect(
        "localhost:7233",
        namespace=NAMESPACE,
        plugins=[
            OpenAIAgentsPlugin(
                model_params=ModelActivityParameters(
                    start_to_close_timeout=timedelta(seconds=30)
                ),
                model_provider=TestModelProvider(LocalOpenAIModel()),
                add_temporal_spans=False,
            ),
            AgentHarnessPlugin(),
        ],
    )
    async with Worker(
        client, task_queue=TASK_QUEUE, workflows=[ParentAgent, ResearchAgent]
    ):
        print(f"READY: namespace={NAMESPACE} queue={TASK_QUEUE}", flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
