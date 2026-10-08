"""Host OpenAI Agents and Pydantic AI on separate queues using the same LiteLLM gateway."""

import argparse
import asyncio
import os
from contextlib import AsyncExitStack
from datetime import timedelta

from agents.models.openai_provider import OpenAIProvider
from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from .providers import LiteLLMSettings, review_model_names
from .security_agent import SECURITY_AGENT
from .workflow import (
    OPENAI_QUEUE,
    PYDANTIC_QUEUE,
    CodeReviewCoordinator,
    CorrectnessReviewer,
    SecurityReviewer,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    settings = LiteLLMSettings.from_env()
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "code-review-demo")
    openai_client = await Client.connect(
        address,
        namespace=namespace,
        plugins=[
            OpenAIAgentsPlugin(
                model_provider=OpenAIProvider(
                    base_url=settings.base_url,
                    api_key=settings.api_key,
                    use_responses=False,
                ),
                model_params=ModelActivityParameters(
                    start_to_close_timeout=timedelta(minutes=3),
                    schedule_to_close_timeout=timedelta(minutes=7),
                    heartbeat_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=2),
                    stream_to_provider=stream_to_provider,
                ),
                observer_factory=harness_observer_factory,
                add_temporal_spans=False,
            ),
            AgentHarnessPlugin(),
        ],
    )
    pydantic_client = await Client.connect(
        address,
        namespace=namespace,
        plugins=[
            PydanticAIPlugin(),
            AgentHarnessPlugin(),
        ],
    )
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            Worker(
                openai_client,
                task_queue=OPENAI_QUEUE,
                workflows=[CodeReviewCoordinator, CorrectnessReviewer],
                activities=[review_model_names],
            )
        )
        await stack.enter_async_context(
            Worker(
                pydantic_client,
                task_queue=PYDANTIC_QUEUE,
                workflows=[SecurityReviewer],
                plugins=[AgentPlugin(SECURITY_AGENT)],
            )
        )
        print(
            f"READY: namespace={namespace}; coordinator/correctness=OpenAI Agents; security=Pydantic AI",
            flush=True,
        )
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
