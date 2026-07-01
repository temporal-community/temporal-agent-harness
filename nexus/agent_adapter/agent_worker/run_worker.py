"""Generic harness agent worker for the Nexus agent-adapter.

NOTE: The reason why this worker has to be separate from the Go-based
      nexus-worker that implements the handler is because we're using
      features that are not yet available in the Python SDK.
TODO: All Nexus workers should be the consolidated into a single worker
      ideally implemented in Python, so we can use the harness interface
      (written in Python) as the Nexus handlers themselves.

Env vars:
    AGENT_WORKFLOW_CLASS    required — import path to the workflow class,
                            e.g. "my_package.my_agent:MyAgentWorkflow"
    AGENT_ACTIVITIES        optional — comma-separated "module:func" specs for
                            activity-backed tools, e.g. "my_pkg.tools:search"
    AGENT_TASK_QUEUE        task queue name (default: "agent"; must match
                            the Go nexus-worker's AGENT_TASK_QUEUE env var)
    GEMINI_API_KEY          enables the Gemini Interactions API plugin
    TEMPORAL_CONFIG_FILE    path to temporal.toml
    TEMPORAL_PROFILE        profile name (default: "default")
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
from typing import Any
from dataclasses import dataclass

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

DEFAULT_TASK_QUEUE = "agent"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentEnvConfigs:
    agent_workflow_name: str
    agent_activities: list[str]
    agent_task_queue: str
    llm_api_key: str


def ensure_agent_env_configs() -> AgentEnvConfigs:
    """Ensure the required env vars are set and return them as a dataclass."""
    agent_workflow_name = os.environ.get("AGENT_WORKFLOW_CLASS")
    if not agent_workflow_name:
        sys.exit(
            "error: AGENT_WORKFLOW_CLASS not set "
            "(e.g. 'my_package.my_agent:MyAgentWorkflow')"
        )

    agent_activities = list(
        filter(None, os.environ.get("AGENT_ACTIVITIES", "").split(","))
    )

    agent_task_queue = os.environ.get("AGENT_TASK_QUEUE", DEFAULT_TASK_QUEUE)

    llm_api_key = os.environ.get("GEMINI_API_KEY", "")

    return AgentEnvConfigs(
        agent_workflow_name=agent_workflow_name,
        agent_activities=agent_activities,
        agent_task_queue=agent_task_queue,
        llm_api_key=llm_api_key,
    )


def _import(spec: str) -> Any:
    """Load an object from a 'dotted.module:Name' spec."""
    module_path, _, name = spec.rpartition(":")
    if not module_path or not name:
        raise ValueError(f"import spec must be 'module.path:Name', got {spec!r}")
    return getattr(importlib.import_module(module_path), name)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    env_configs = ensure_agent_env_configs()

    workflow_spec = env_configs.agent_workflow_name
    workflow_class = _import(workflow_spec)

    activities = [_import(spec) for spec in env_configs.agent_activities]

    plugins = []
    if env_configs.llm_api_key:
        from google.genai import Client as GeminiClient
        from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin
        plugins.append(GoogleGenAIPlugin(GeminiClient(api_key=env_configs.llm_api_key)))

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=plugins,
        data_converter=pydantic_data_converter,
    )

    worker = Worker(
        client,
        task_queue=env_configs.agent_task_queue,
        workflows=[workflow_class],
        activities=activities,
    )

    logger.info(
        "nexus agent-adapter worker ready: namespace=%s taskQueue=%s workflow=%s",
        client.namespace,
        env_configs.agent_task_queue,
        workflow_class.__name__,
    )

    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
