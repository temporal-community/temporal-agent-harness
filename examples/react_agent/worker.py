"""Worker for the ReAct agent.

Run from the repo root with:
    uv run --group examples python -m examples.react_agent.worker

Hosts the ReactAgent workflow and its four tool activities (get_ip_address,
get_location_info, get_coordinates, get_weather). Each tool is a harness activity tool
(`@agent.activity_tool_defn`) doing real HTTP, so its activity body is registered here via
`agent.tool_activity(...)` (bundled as ALL_ACTIVITIES). The OpenAI Agents plugin registers the
model activities (including the streaming one) itself.

The plugin is wired for the HARNESS STREAMING PATH:
  * ``model_params.stream_to_provider=stream_to_provider`` — reads each streamed model call's
    per-turn stream context off the run context the workflow threads in (the workflow calls
    ``Runner.run_streamed(..., context=self._runner)``), and
  * ``observer_factory=harness_observer_factory`` — turns that context into the observer that
    translates raw OpenAI events into the harness turn-stream vocabulary live.
Drop either hook, or omit the ``context=self._runner`` on ``run_streamed``, and there is no
stream target — the streamed call raises unless you also set ``model_params.streaming_topic``
(the plugin's plain raw-topic fallback, not wired here).

and for the NON-STREAMING PATH (``REACT_AGENT_STREAM=0``):
  * ``model_params.model_call_observer_provider=model_call_observer_provider`` — brackets each
    ``Runner.run`` model call in workflow code, publishing ``model_interaction_started`` /
    ``…_ended`` (with token usage) and ``tool_requested``. Those are facts about the turn, not
    streaming artifacts, so turning tokens off must not turn them off. It reads the same run
    context, so the workflow passes ``context=self._runner`` on ``Runner.run`` too.

Env vars (set in the repo-root .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required — the agent calls the OpenAI API
    REACT_AGENT_TASK_QUEUE                    task queue to poll (default: react-agent)
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
from agents.mcp import MCPServerStdio

from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
    StatelessMCPServerProvider
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    model_call_observer_provider,
    stream_to_provider,
)

from .tool_activities import ALL_ACTIVITIES
from .workflow import TASK_QUEUE, ReactAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("REACT_AGENT_TASK_QUEUE", TASK_QUEUE)
    MCP_SERVER_NAME = "f1-data"
    F1_MCP_SERVER_HOME = os.environ.get(
        "F1_MCP_SERVER_HOME",
        os.path.expanduser("~/Projects/Temporal/AI/MCP/f1-mcp-server"),
    )

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    def _f1_server_factory() -> MCPServerStdio:
        # The F1 server is Node.js but shells out to python3 for FastF1. Activating
        # its venv ensures the child python3 finds fastf1, pandas, numpy on PATH.
        launch = (
            f"source {F1_MCP_SERVER_HOME}/.venv/bin/activate"
            f" && node {F1_MCP_SERVER_HOME}/build/index.js"
        )
        return MCPServerStdio(
            name=MCP_SERVER_NAME,
            params={
                "command": "bash",
                "args": ["-c", launch],
            },
            cache_tools_list=True,
        )

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            # Streaming leans on activity heartbeats to notice a stuck model call; keep the
            # heartbeat timeout well under the overall start-to-close.
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            # The harness streaming seam: route streamed events to the in-flight turn.
            stream_to_provider=stream_to_provider,
            # Its non-streaming counterpart: bracket every `Runner.run` model call
            # workflow-side, so model_interaction_started/ended (with token usage) and
            # tool_requested land on the turn stream whether or not tokens streamed.
            model_call_observer_provider=model_call_observer_provider,
        ),
        mcp_server_providers=[
            StatelessMCPServerProvider(
                name=MCP_SERVER_NAME,
                server_factory=_f1_server_factory,
            ),
        ],
        observer_factory=harness_observer_factory,
    )

    # The plugin supplies its own (OpenAI-aware, pydantic-compatible) data converter.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[plugin])

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[ReactAgentWorkflow],
        # The four location/weather tool activity bodies. The OpenAI model activities
        # (incl. invoke_model_activity_streaming) are registered by the plugin. The ask_user
        # callback tool has no activity body — it's fulfilled by the client — so nothing to
        # register for it here.
        activities=ALL_ACTIVITIES,
    )
    print(
        f"ReAct agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
