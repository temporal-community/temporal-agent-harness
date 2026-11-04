"""Read-only local configuration, Temporal, and HTTP diagnostics."""
from __future__ import annotations

import asyncio
import math
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from temporalio.client import Client
from temporalio.envconfig import ClientConfig

from temporal_agent_harness.development import api_checks
from temporal_agent_harness.utils.task_queue import task_queue_name
from temporal_agent_harness.web.registry import load_agent_registry
from temporal_agent_harness.web.task_queue_status import describe_task_queue_workers


async def inspect_temporal(queues: list[str], timeout: float) -> list[dict[str, Any]]:
    async def probe() -> list[dict[str, Any]]:
        config = ClientConfig.load_client_connect_config()
        config.setdefault("target_host", "localhost:7233")
        client = await Client.connect(**config)
        statuses = await describe_task_queue_workers(client, queues)
        return [{"check": "temporal", "ok": True, "detail": f"namespace {client.namespace}"}, *[
            {"check": f"queue:{name}", "ok": status.status == "ready", "detail": status.status}
            for name, status in statuses.by_task_queue.items()
        ]]
    try:
        return await asyncio.wait_for(probe(), timeout=timeout)
    except Exception as error:
        return [{"check": "temporal", "ok": False,
                 "detail": f"{type(error).__name__}: check Temporal connectivity and TEMPORAL_* configuration"}]


def diagnose(registry: Path | None, api_url: str, timeout: float = 5) -> dict[str, Any]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive")
    versions = {}
    for name in ("temporal-agent-harness", "temporalio", "pydantic"):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = "not installed"
    checks = []
    queues = [task_queue_name("session-manager")]
    if registry is not None:
        try:
            agents = load_agent_registry(registry).agents
            queues.extend(agent.task_queue for agent in agents)
            checks.append({"check": "registry", "ok": True, "detail": f"{len(agents)} agents; referenced schemas loaded"})
        except (OSError, ValueError, AttributeError, TypeError) as error:
            checks.append({"check": "registry", "ok": False, "detail": str(error)})
    checks.extend(asyncio.run(inspect_temporal(queues, timeout)))
    checks.extend(api_checks(api_url, timeout))
    return {"ok": all(c["ok"] for c in checks), "versions": versions, "checks": checks}
