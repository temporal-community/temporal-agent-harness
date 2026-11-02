import asyncio
import os

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.typesafe import TypeSafePlugin
from temporalio.worker import Worker
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy
from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporal_agent_harness.utils.task_queue import task_queue_name
from examples.typesafe_hello.workflow import TypeSafeHelloAgent


async def main():
    async with AsyncTypeSafeClient(retry=RetryPolicy(max_retries=0)) as provider:
        client = await Client.connect(**ClientConfig.load_client_connect_config(),
            plugins=[TypeSafePlugin(provider), AgentHarnessPlugin()])
        task_queue = (
            os.environ.get("TYPESAFE_HELLO_TASK_QUEUE") or task_queue_name("typesafe-hello")
        )
        await Worker(client, task_queue=task_queue, workflows=[TypeSafeHelloAgent]).run()


if __name__ == "__main__":
    asyncio.run(main())
