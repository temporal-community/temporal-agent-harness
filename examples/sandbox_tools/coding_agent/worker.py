"""Worker for the sandboxed coding agent.

Run from the repo root with:
    uv run --extra sandbox --group examples python -m examples.sandbox_tools.coding_agent.worker

Hosts SandboxedCodingAgentWorkflow plus its sandbox lifecycle activities (and the microsandbox
egress backend provider stub), its six sandboxed tools' activities (bash/read/write/edit/grep/glob),
and ``run_subagent_turn`` so a parent can drive another instance of this same workflow as a child.
The OpenAI Agents plugin is registered because the agent drives the OpenAI Agents SDK; the plugin
auto-registers its model activities (including the streaming one). Run `just build-sandbox` once
before starting this worker — runtime never builds the sandbox image implicitly
(SandboxConfig.require_prebuilt).

The plugin is wired for the HARNESS STREAMING PATH:
  * ``model_params.stream_to_provider=stream_to_provider`` — resolves each streamed model
    call's per-turn stream context ambiently off the running workflow, and
  * ``observer_factory=harness_observer_factory`` — turns that context into the observer that
    translates raw OpenAI events into the harness turn-stream vocabulary live.
Drop either one and the UI stops showing tokens as they arrive.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required — the agent calls the OpenAI API
    SANDBOX_BACKEND                           "local" (default, in-process tools) or "microsandbox"
    SANDBOXED_CODING_AGENT_TASK_QUEUE         task queue to poll (default: sandboxed-coding-agent)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import timedelta

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
)
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.sandbox.activities import sandbox_activities
from temporal_agent_harness.harness.subagent_activities import SubagentActivities

from .egress import PROVIDER_NAME, microsandbox_openai_egress
from .tools import SANDBOX, SANDBOXED_CODING_TOOLS
from .workflow import TASK_QUEUE, SandboxedCodingAgentWorkflow


def worker_activities(client: Client) -> list:
    """Activities this worker hosts, beside the ones the OpenAI Agents plugin registers.

    ``run_subagent_turn`` is how one agent drives another. Subagents are this same workflow
    on this same queue, so registering it once serves every tier the depth cap allows.
    """
    return [
        *sandbox_activities({PROVIDER_NAME: microsandbox_openai_egress}),
        *(agent.tool_activity(tool) for tool in SANDBOXED_CODING_TOOLS),
        SubagentActivities(client).run_subagent_turn,
    ]


def openai_agents_plugin() -> OpenAIAgentsPlugin:
    """The plugin this worker runs with — and so the one a replay of its history needs.

    Wired for the HARNESS STREAMING PATH (see the module docstring). It also decides that model
    calls leave the workflow as activities at all, which is half of any recorded session's
    command sequence, so a replay test that built its own would be replaying a different worker.
    """
    return OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(minutes=3),
            heartbeat_timeout=timedelta(seconds=30),
            stream_to_provider=stream_to_provider,
        ),
        observer_factory=harness_observer_factory,
    )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("SANDBOXED_CODING_AGENT_TASK_QUEUE", TASK_QUEUE)

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    plugin = openai_agents_plugin()

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[plugin])

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[SandboxedCodingAgentWorkflow],
        activities=worker_activities(client),
    )
    sandbox_backend = (
        "local"
        if SANDBOX.backend == "local"
        else (SANDBOX.backend if isinstance(SANDBOX.backend, str) else "microsandbox")
    )
    print(
        f"sandboxed-coding-agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue} "
        f"sandboxBackend={sandbox_backend!r}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
