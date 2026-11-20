"""Run the Worker for the Claude Agent SDK hello-world example."""

import asyncio

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from examples.claude_agent_sdk_hello.workflow import TASK_QUEUE, ClaudeAgentSDKHelloWorkflow
from temporal_agent_kit.ai_sdks.claude_agent_sdk import ClaudeAgentSDKPlugin
from temporal_agent_kit.plugin import AgentKitPlugin
from temporal_agent_kit.utils.task_queue import task_queue_name


async def main() -> None:
    client = await Client.connect(**ClientConfig.load_client_connect_config())
    worker = Worker(
        client,
        task_queue=task_queue_name(TASK_QUEUE),
        workflows=[ClaudeAgentSDKHelloWorkflow],
        plugins=[ClaudeAgentSDKPlugin(), AgentKitPlugin()],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
