"""Worker for the Claude ReAct agent.

Run from the repo root with:
    uv run --group examples --extra claude-agent-sdk python -m examples.claude_react_agent.worker

Env vars (repo-root .env.local):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    ANTHROPIC_API_KEY                         optional — else Claude Code uses its own login
    CLAUDE_REACT_AGENT_TASK_QUEUE             task queue (default: claude-react-agent)
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

from examples.react_agent.human_tools import HUMAN_TOOLS
from examples.react_agent.tool_activities import ALL_TOOLS
from temporal_agent_harness.plugin import AgentHarnessPlugin

from .workflow import TASK_QUEUE, ClaudeReactAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    task_queue = os.environ.get("CLAUDE_REACT_AGENT_TASK_QUEUE", TASK_QUEUE)
    cwd = tempfile.mkdtemp(prefix="claude-react-")

    connect_config = ClientConfig.load_client_connect_config()
    # The harness plugin registers the tool activity bodies (and skips the ask_user callback tool);
    # ClaudeAgentPlugin goes on the Worker only.
    client = await Client.connect(
        **connect_config, plugins=[AgentHarnessPlugin(tools=[*ALL_TOOLS, *HUMAN_TOOLS])]
    )
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[ClaudeReactAgentWorkflow],
        plugins=[ClaudeAgentPlugin(ClaudeAgentSdkRunner(cwd=cwd))],
        max_concurrent_activities=8,
    )
    print(f"Claude ReAct worker ready: taskQueue={task_queue} cwd={cwd}", flush=True)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
