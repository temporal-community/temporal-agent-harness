"""Worker for the hello-world Claude Agent SDK agent.

Run from the python/ directory with:
    uv run --group examples python -m examples.claude_agent_sdk_hello.worker

Hosts only the ClaudeAgentSDKHelloAgent workflow. Claude Code runs in a subprocess of this Worker,
in its working directory. `ClaudeAgentSDKPlugin` runs it, and `AgentHarnessPlugin` adds the harness.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    ANTHROPIC_API_KEY                         Claude Code's credentials
    CLAUDE_AGENT_SDK_HELLO_TASK_QUEUE         task queue to poll (default: claude-agent-sdk-hello)
    TASK_QUEUE_PREFIX                         put in front of the default queue's name, if set
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.claude_agent_sdk import ClaudeAgentSDKPlugin
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporal_agent_harness.utils.task_queue import task_queue_name

from examples.claude_agent_sdk_hello.workflow import ClaudeAgentSDKHelloAgentWorkflow

TASK_QUEUE = "claude-agent-sdk-hello"


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("CLAUDE_AGENT_SDK_HELLO_TASK_QUEUE") or task_queue_name(
        TASK_QUEUE
    )

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config, plugins=[ClaudeAgentSDKPlugin(), AgentHarnessPlugin()]
    )

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[ClaudeAgentSDKHelloAgentWorkflow],
    )
    print(
        f"Claude Agent SDK hello agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
