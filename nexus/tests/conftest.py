# ABOUTME: Fixtures for nexus/tests/ — real-server, real-Nexus-wire integration tests, as
# opposed to tests/nexus/test_agent_registry.py's workflow-only tests against a time-skipping
# WorkflowEnvironment (no server, no Nexus endpoint).
#
# SERVER REQUIREMENT: these tests need a temporal-server binary built from `main` with
# standalone Nexus operations enabled (nexus/devserver/dynamicconfig/development.yaml) — not
# yet in any released server; a stock `temporal server start-dev` will fail these tests with
# "unknown method StartNexusOperationExecution". Build the binary once with:
#     make -C nexus/slack_connector install-dev-server
# which installs it to `$(go env GOPATH)/bin/temporal-server` (or point TEMPORAL_SERVER_BINARY
# at any prebuilt binary). Tests here are SKIPPED, not failed, when no binary is found, so a
# plain `pytest`/`uv run pytest` run elsewhere in the repo stays green without it.
#
# LOOP SCOPE: both fixtures below are session-scoped and must run on the SAME event loop as the
# tests that use them (loop_scope="session" on the fixture, and `pytestmark =
# pytest.mark.asyncio(loop_scope="session")` in the test module) — NOT pytest-asyncio's default
# per-test loop. create_agent_registry_worker's polling loop is a background asyncio Task
# scheduled on whichever loop was running when its `async with` block was entered; if tests ran
# on a separate (function-scoped) loop, that task would simply never get scheduled once control
# returned to the test, and every Nexus call would sit until its schedule_to_close_timeout fired
# — no error, no log line, just a silent ~30s hang. Sharing one loop for setup and test bodies
# is what keeps the worker's poller actually running while a test awaits on it.

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from google.protobuf.duration_pb2 import Duration
from temporalio.api.nexus.v1 import EndpointSpec, EndpointTarget
from temporalio.api.operatorservice.v1 import CreateNexusEndpointRequest
from temporalio.api.workflowservice.v1 import RegisterNamespaceRequest
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError

from agent_registry.run_worker import (
    create_agent_registry_worker,
    ensure_agent_registry_workflow_started,
)

_NEXUS_DIR = Path(__file__).resolve().parent.parent  # nexus/
_DEVSERVER_CONFIG = _NEXUS_DIR / "devserver" / "config.yaml"
_NAMESPACE = "default"
_ADDRESS = "localhost:7233"


def _find_temporal_server_binary() -> str | None:
    """Locate a temporal-server binary: $TEMPORAL_SERVER_BINARY, else `go env GOPATH`/bin —
    where `make -C nexus/slack_connector install-dev-server` installs it."""
    env_path = os.environ.get("TEMPORAL_SERVER_BINARY")
    if env_path:
        return env_path if Path(env_path).is_file() else None
    try:
        gopath = subprocess.run(
            ["go", "env", "GOPATH"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    candidate = Path(gopath) / "bin" / "temporal-server"
    return str(candidate) if candidate.is_file() else None


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def devserver_client() -> AsyncIterator[Client]:
    """Start the custom-built temporal-server (standalone-Nexus-capable) for the whole test
    session, register the default namespace, and hand back a connected client."""
    binary = _find_temporal_server_binary()
    if binary is None:
        pytest.skip(
            "no temporal-server binary found — build one with "
            "`make -C nexus/slack_connector install-dev-server` "
            "(or set TEMPORAL_SERVER_BINARY), then re-run"
        )

    proc = subprocess.Popen(
        [binary, "--config-file", str(_DEVSERVER_CONFIG), "--allow-no-auth", "start"],
        cwd=_NEXUS_DIR / "devserver",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        client = await _connect_with_retries()
        await _ensure_namespace_registered(client)
        yield client
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def _connect_with_retries(*, timeout_seconds: float = 30.0) -> Client:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return await Client.connect(
                _ADDRESS, namespace=_NAMESPACE, data_converter=pydantic_data_converter
            )
        except Exception as e:  # noqa: BLE001 - retrying on anything is the point here
            last_error = e
            time.sleep(0.5)
    raise RuntimeError(f"temporal-server never became reachable at {_ADDRESS}") from last_error


async def _ensure_namespace_registered(client: Client) -> None:
    try:
        await client.workflow_service.register_namespace(
            RegisterNamespaceRequest(
                namespace=_NAMESPACE,
                workflow_execution_retention_period=Duration(seconds=24 * 60 * 60),
            )
        )
    except RPCError as e:
        if "already exists" not in str(e).lower():
            raise


async def create_nexus_endpoint(client: Client, name: str, task_queue: str) -> None:
    """Register a Nexus endpoint routing ``name`` to ``task_queue`` in the default namespace —
    the Python-client equivalent of `temporal operator nexus endpoint create`."""
    await client.operator_service.create_nexus_endpoint(
        CreateNexusEndpointRequest(
            spec=EndpointSpec(
                name=name,
                target=EndpointTarget(
                    worker=EndpointTarget.Worker(namespace=_NAMESPACE, task_queue=task_queue)
                ),
            )
        )
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def agent_registry_endpoint(devserver_client: Client) -> AsyncIterator[str]:
    """Start the (singleton) AgentRegistryWorkflow + its worker ONCE for the whole session and
    register a Nexus endpoint fronting it — mirrors how it's actually deployed (one registry,
    one task queue, for the environment's lifetime), not one per test.

    ``AGENT_REGISTRY_WORKFLOW_ID`` is a fixed id (``ensure_agent_registry_workflow_started``
    uses ``USE_EXISTING``), so giving each test its own throwaway task queue — as if the
    registry were per-test — silently reuses this same singleton workflow but leaves it bound
    to whichever test's worker started it first; once that test's worker shuts down, the next
    test's signals/queries to it just hang forever waiting for a poller that's gone. Tests
    sharing this one fixture and using distinct ``agent_key``s per test avoids that entirely."""
    task_queue = "agent-registry-nexus-test"
    endpoint = "agent-registry-endpoint-nexus-test"
    await create_nexus_endpoint(devserver_client, endpoint, task_queue)
    await ensure_agent_registry_workflow_started(devserver_client, task_queue=task_queue)
    async with create_agent_registry_worker(devserver_client, task_queue=task_queue):
        yield endpoint
