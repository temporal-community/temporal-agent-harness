# ABOUTME: Tests all caller paths against one live Nexus tool service.

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import anyio
import nexusrpc.handler
import temporalio.nexus
from mcp import types
from mcp.client import Client
from nexus_mcp import NexusTasksClientExtension
from nexus_mcp.authoring import (
    MCPOverNexusServiceHandler,
    nexus_mcp_operation,
    nexus_mcp_tool,
)
from nexus_mcp.execution import StandaloneNexusExecutor, WorkflowNexusExecutor
from nexus_mcp.frontends import (
    MODERN_PROTOCOL_VERSION,
    NexusMCPBridge,
)
from nexus_mcp.resolver import NexusToolResolver
from nexus_mcp.tasks import CreateTaskResult, get_task
from pydantic import BaseModel
from temporalio import workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

_DEV_SERVER_VERSION = "v1.7.1-system-nexus-operations"


class _DelayedGreetingInput(BaseModel):
    name: str
    delay_seconds: float = 0.1


class _DelayedGreetingOutput(BaseModel):
    message: str


@workflow.defn(sandboxed=False)
class _DelayedGreetingWorkflow:
    @workflow.run
    async def run(self, input: _DelayedGreetingInput) -> _DelayedGreetingOutput:
        await workflow.sleep(input.delay_seconds)
        return _DelayedGreetingOutput(message=f"Hello later, {input.name}")


@nexusrpc.handler.service_handler(name="matrix")
class _MatrixService(MCPOverNexusServiceHandler):
    @nexus_mcp_tool
    async def greet(self, name: str) -> dict[str, str]:
        """Greet one person."""
        return {"message": f"Hello, {name}"}

    @nexus_mcp_operation
    @temporalio.nexus.workflow_run_operation
    async def delayed_greet(
        self,
        ctx: temporalio.nexus.WorkflowRunOperationContext,
        input: _DelayedGreetingInput,
    ) -> temporalio.nexus.WorkflowHandle[_DelayedGreetingOutput]:
        """Greet one person after a durable delay."""
        return await ctx.start_workflow(
            _DelayedGreetingWorkflow.run,
            input,
            id=f"matrix-delayed-greeting-{ctx.request_id}",
        )


@dataclass
class _WorkflowInput:
    endpoint: str


@workflow.defn(sandboxed=False)
class _WorkflowCaller:
    @workflow.run
    async def run(self, input: _WorkflowInput) -> dict[str, Any]:
        resolver = NexusToolResolver(
            {"matrix": input.endpoint}, WorkflowNexusExecutor()
        )
        tools = await resolver.list_tools()
        result = await resolver.call_tool("matrix_greet", {"name": "Workflow"})
        delayed = await resolver.call_tool(
            "matrix_delayed_greet",
            {"name": "Workflow", "delay_seconds": 0.01},
        )
        return {
            "tools": [tool.name for tool in tools],
            "result": result.structured_content,
            "delayed": delayed.structured_content,
        }


async def _call_mcp_bridge(
    bridge: NexusMCPBridge,
    *,
    mode: str,
    name: str,
    use_tasks: bool = False,
) -> dict[str, Any]:
    extensions = [NexusTasksClientExtension()] if use_tasks else None
    async with Client(
        bridge,
        mode=mode,
        extensions=extensions,
        raise_exceptions=True,
    ) as client:
        tools = await client.list_tools()
        result = await client.call_tool("matrix_greet", {"name": name})
        arguments = {"name": name, "delay_seconds": 0.01}
        if use_tasks:
            task = await client.session.call_tool(
                "matrix_delayed_greet",
                arguments,
                allow_claimed=True,
            )
            assert isinstance(task, CreateTaskResult)
            for _ in range(200):
                current = await get_task(client.session, task.task_id)
                if current.status == "completed":
                    assert current.result is not None
                    delayed = types.CallToolResult.model_validate(current.result)
                    break
                await anyio.sleep(0.025)
            else:
                raise AssertionError("The MCP task did not complete.")
        else:
            delayed = await client.call_tool("matrix_delayed_greet", arguments)
        return {
            "tools": [tool.name for tool in tools.tools],
            "result": result.structured_content,
            "delayed": delayed.structured_content,
        }


async def test_workflow_stateful_and_stateless_callers() -> None:
    endpoint = f"matrix-endpoint-{uuid.uuid4()}"
    task_queue = f"matrix-task-queue-{uuid.uuid4()}"
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        dev_server_download_version=_DEV_SERVER_VERSION,
        dev_server_extra_args=[
            "--dynamic-config-value",
            "nexusoperation.enableStandalone=true",
            "--dynamic-config-value",
            "history.enableChasm=true",
            "--dynamic-config-value",
            "history.enableTransitionHistory=true",
            "--dynamic-config-value",
            "history.enableCHASMCallbacks=true",
            "--dynamic-config-value",
            "history.enableUpdateCallbacks=true",
            "--dynamic-config-value",
            'system.system.refreshNexusEndpointsMinWait="0s"',
        ],
    ) as env:
        await env.create_nexus_endpoint(endpoint, task_queue)
        resolver = NexusToolResolver(
            {"matrix": endpoint},
            StandaloneNexusExecutor(env.client),
        )

        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[_WorkflowCaller, _DelayedGreetingWorkflow],
            nexus_service_handlers=[_MatrixService()],
        ):
            workflow_result = await env.client.execute_workflow(
                _WorkflowCaller.run,
                _WorkflowInput(endpoint),
                id=str(uuid.uuid4()),
                task_queue=task_queue,
            )
            bridge = NexusMCPBridge(resolver, task_poll_interval_ms=10)
            stateful_result = await _call_mcp_bridge(
                bridge,
                mode="legacy",
                name="Stateful",
            )
            stateless_result = await _call_mcp_bridge(
                bridge,
                mode=MODERN_PROTOCOL_VERSION,
                name="Stateless",
                use_tasks=True,
            )

    assert workflow_result == {
        "tools": ["matrix_delayed_greet", "matrix_greet"],
        "result": {"message": "Hello, Workflow"},
        "delayed": {"message": "Hello later, Workflow"},
    }
    assert stateful_result == {
        "tools": ["matrix_delayed_greet", "matrix_greet"],
        "result": {"message": "Hello, Stateful"},
        "delayed": {"message": "Hello later, Stateful"},
    }
    assert stateless_result == {
        "tools": ["matrix_delayed_greet", "matrix_greet"],
        "result": {"message": "Hello, Stateless"},
        "delayed": {"message": "Hello later, Stateless"},
    }
