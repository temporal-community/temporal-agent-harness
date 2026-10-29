# ABOUTME: Tests direct workflow access and the MCP-to-Nexus bridge.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from nexus_mcp import NexusTasksClientExtension
from nexus_mcp.execution import StandaloneNexusExecutor, WorkflowNexusExecutor
from nexus_mcp.frontends import MODERN_PROTOCOL_VERSION, NexusMCPBridge
from nexus_mcp.resolver import NexusTask, NexusToolResolver, RequestContext
from nexus_mcp.tasks import CreateTaskResult, cancel_task, get_task, update_task


def _tool(service: str = "echo-service", name: str | None = None) -> dict[str, Any]:
    return {
        "name": name or f"{service}_echo",
        "description": "Echo text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }


class _FakeNexusClient:
    def __init__(self, service: str, calls: list[dict[str, Any]]) -> None:
        self._service = service
        self._calls = calls

    async def execute_operation(
        self,
        operation: str,
        argument: Any,
        **options: Any,
    ) -> Any:
        self._calls.append(
            {
                "service": self._service,
                "operation": operation,
                "argument": argument,
                **options,
            }
        )
        if operation == "list_tools":
            name = f"{self._service}_echo"
            return {"tools": [_tool(self._service, name)], "routes": {name: "echo"}}
        return {"service": self._service, "arguments": argument}


class _FakeTemporalClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_nexus_client(self, service: str, endpoint: str) -> _FakeNexusClient:
        return _FakeNexusClient(service, self.calls)


def _resolver(client: _FakeTemporalClient | None = None) -> NexusToolResolver:
    return NexusToolResolver(
        {"echo-service": "tools-endpoint"},
        StandaloneNexusExecutor(client or _FakeTemporalClient()),  # type: ignore[arg-type]
    )


class _TaskExecutor:
    def __init__(self) -> None:
        self.created_at = datetime(2026, 9, 4, tzinfo=UTC)
        self.cancelled: list[str] = []
        self.contexts: list[RequestContext] = []

    async def execute(self, **kwargs: Any) -> Any:
        self.contexts.append(kwargs["context"])
        if kwargs["operation"] == "list_tools":
            return {
                "tools": [_tool(name="public_echo")],
                "routes": {"public_echo": "private_operation"},
            }
        return {"mode": "complete"}

    async def start_task(self, **kwargs: Any) -> NexusTask:
        self.contexts.append(kwargs["context"])
        return NexusTask(
            task_id="nexus-operation-1",
            service=kwargs["service"],
            endpoint=kwargs["endpoint"],
            operation=kwargs["operation"],
            status="working",
            created_at=self.created_at,
            last_updated_at=self.created_at,
        )

    async def get_task(self, task_id: str) -> NexusTask:
        if task_id != "nexus-operation-1":
            raise ValueError("Task not found")
        return NexusTask(
            task_id=task_id,
            service="echo-service",
            endpoint="tools-endpoint",
            operation="private_operation",
            status="completed",
            created_at=self.created_at,
            last_updated_at=self.created_at,
            result={"answer": 42},
        )

    async def cancel_task(self, task_id: str) -> None:
        self.cancelled.append(task_id)


@patch("nexus_mcp.execution.workflow.workflow.create_nexus_client")
async def test_workflow_resolver_discovers_an_exact_route_before_calling(
    create_nexus_client: MagicMock,
) -> None:
    nexus_client = MagicMock()
    nexus_client.execute_operation = AsyncMock(
        side_effect=[
            {
                "tools": [_tool(name="public_echo")],
                "routes": {"public_echo": "private_operation"},
            },
            {"text": "hello"},
        ]
    )
    create_nexus_client.return_value = nexus_client
    resolver = NexusToolResolver(
        {"echo-service": "tools-endpoint"}, WorkflowNexusExecutor()
    )

    result = await resolver.call_tool("public_echo", {"text": "hello"})

    assert result.structured_content == {"text": "hello"}
    assert [
        call.args[0] for call in nexus_client.execute_operation.await_args_list
    ] == [
        "list_tools",
        "private_operation",
    ]


async def test_resolver_rejects_a_name_that_only_matches_a_service_prefix() -> None:
    resolver = _resolver()

    with pytest.raises(ValueError, match="not registered"):
        await resolver.call_tool("echo-service_not_advertised", {})


async def test_resolver_rejects_duplicate_public_tool_names() -> None:
    class DuplicateExecutor:
        async def execute(self, **kwargs: Any) -> Any:
            return {
                "tools": [_tool(name="shared_name")],
                "routes": {"shared_name": "private_operation"},
            }

    resolver = NexusToolResolver(
        {"one": "one-endpoint", "two": "two-endpoint"},
        DuplicateExecutor(),
    )

    with pytest.raises(ValueError, match="More than one Nexus service"):
        await resolver.list_tools()


@pytest.mark.parametrize("mode", ["legacy", MODERN_PROTOCOL_VERSION, "auto"])
async def test_sdk_bridge_supports_legacy_and_modern_clients(mode: str) -> None:
    bridge = NexusMCPBridge(_resolver(), instructions="Use the echo tool.")

    async with Client(bridge, mode=mode, raise_exceptions=True) as client:
        tools = await client.list_tools()
        result = await client.call_tool("echo-service_echo", {"text": "hello"})

        assert [tool.name for tool in tools.tools] == ["echo-service_echo"]
        assert result.structured_content == {
            "service": "echo-service",
            "arguments": {"text": "hello"},
        }


async def test_unknown_tool_is_an_mcp_invalid_params_error() -> None:
    bridge = NexusMCPBridge(_resolver())

    async with Client(bridge, mode=MODERN_PROTOCOL_VERSION) as client:
        with pytest.raises(MCPError) as error:
            await client.call_tool("echo-service_missing", {})

    assert error.value.code == -32602


async def test_tasks_can_be_polled_or_resolved_by_the_client_extension() -> None:
    executor = _TaskExecutor()
    bridge = NexusMCPBridge(
        NexusToolResolver({"echo-service": "tools-endpoint"}, executor),
        task_ttl_ms=None,
        task_poll_interval_ms=1,
    )

    async with Client(
        bridge,
        mode=MODERN_PROTOCOL_VERSION,
        extensions=[NexusTasksClientExtension()],
        raise_exceptions=True,
    ) as client:
        task = await client.session.call_tool(
            "public_echo",
            {"text": "hello"},
            meta={"io.temporal/idempotencyKey": "stable-task-id"},
            allow_claimed=True,
        )
        assert isinstance(task, CreateTaskResult)
        assert task.model_dump(by_alias=True, exclude_none=True)["ttlMs"] is None
        current = await get_task(client.session, task.task_id)
        await update_task(client.session, task.task_id, {"unused": "value"})
        await cancel_task(client.session, task.task_id)
        result = await client.call_tool("public_echo", {"text": "hello"})

    assert current.status == "completed"
    assert current.result is not None
    assert current.result["structuredContent"] == {"answer": 42}
    assert result.structured_content == {"answer": 42}
    assert executor.cancelled == ["nexus-operation-1"]
    assert executor.contexts[0].idempotency_key is None
    assert executor.contexts[1].idempotency_key == "stable-task-id"


@pytest.mark.parametrize("mode", ["legacy", MODERN_PROTOCOL_VERSION])
async def test_clients_without_tasks_use_normal_results(mode: str) -> None:
    executor = _TaskExecutor()
    bridge = NexusMCPBridge(
        NexusToolResolver({"echo-service": "tools-endpoint"}, executor)
    )

    async with Client(bridge, mode=mode, raise_exceptions=True) as client:
        result = await client.call_tool("public_echo", {})

    assert result.structured_content == {"mode": "complete"}


async def test_bridge_serves_a_stateless_http_tool_call() -> None:
    bridge = NexusMCPBridge(_resolver())
    app = bridge.streamable_http_app(stateless_http=True, json_response=True)
    request = {
        "jsonrpc": "2.0",
        "id": "request-1",
        "method": "tools/call",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
            "name": "echo-service_echo",
            "arguments": {"text": "hello"},
        },
    }
    headers = {
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "echo-service_echo",
        "Accept": "application/json, text/event-stream",
    }

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost:8000",
        ) as client:
            response = await client.post("/mcp", headers=headers, json=request)

    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"] == {
        "service": "echo-service",
        "arguments": {"text": "hello"},
    }
