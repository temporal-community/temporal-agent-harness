"""Worker for the hello-world Claude Agent SDK agent.

Run from the repo root with:
    uv run --group examples --extra claude-agent-sdk python -m examples.claude_agent_hello.worker

Hosts only the ClaudeHelloAgent workflow. ``ClaudeAgentPlugin`` registers the model-segment and
tool-step Activities; ``AgentHarnessPlugin`` supplies the harness's data converter and activities.
Claude Code itself needs credentials on the Worker (ANTHROPIC_API_KEY, or Bedrock/Vertex config).

Env vars:
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    CLAUDE_HELLO_TASK_QUEUE                   task queue to poll (default: claude-hello)
    CLAUDE_HELLO_CWD                          Claude Code's working directory (default: a temp dir)
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from temporalio.claude_agent_sdk import ClaudeAgentPlugin, ClaudeAgentSdkRunner
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.plugin import AgentHarnessPlugin

from .workflow import TASK_QUEUE, ClaudeHelloAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    task_queue = os.environ.get("CLAUDE_HELLO_TASK_QUEUE", TASK_QUEUE)
    cwd = os.environ.get("CLAUDE_HELLO_CWD") or tempfile.mkdtemp(prefix="claude-hello-")

    # Harness plugin on the Client; the Claude plugin on the Worker (the PR says: one place, not
    # both, for ClaudeAgentPlugin).
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[AgentHarnessPlugin()])
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[ClaudeHelloAgentWorkflow],
        plugins=[ClaudeAgentPlugin(ClaudeAgentSdkRunner(cwd=cwd))],
        # Each model step starts a Claude Code process (~270 MB); cap parallel steps.
        max_concurrent_activities=4,
    )
    print(f"Claude hello worker ready: taskQueue={task_queue} cwd={cwd}", flush=True)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
