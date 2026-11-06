# ABOUTME: One local Temporal dev server per test module for the LangGraph tests.

from collections.abc import AsyncIterator

import pytest_asyncio
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client() -> AsyncIterator[Client]:
    """One local dev server for the module; conftest points it at the pinned CLI under Bazel."""
    env = await WorkflowEnvironment.start_local()
    try:
        yield env.client
    finally:
        await env.shutdown()
