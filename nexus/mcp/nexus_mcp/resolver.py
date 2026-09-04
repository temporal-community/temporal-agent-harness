"""Protocol-neutral tool discovery and dispatch for Nexus-backed tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

import pydantic
from mcp import types

from nexus_mcp.authoring import LIST_TOOLS_OPERATION


@dataclass(frozen=True)
class RequestContext:
    """Store request data that a frontend passes to an executor.

    The MCP bridge reads this data from the SDK request context. A direct workflow
    caller can leave it empty.
    """

    request_id: str | int | None = None
    protocol_version: str | None = None
    client_info: Mapping[str, Any] = field(default_factory=dict)
    client_capabilities: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    nexus_headers: Mapping[str, str] = field(default_factory=dict)
    idempotency_key: str | None = None


class NexusOperationExecutor(Protocol):
    """Run one Nexus operation from a specific execution context."""

    async def execute(
        self,
        *,
        service: str,
        endpoint: str,
        operation: str,
        argument: Any,
        context: RequestContext,
    ) -> Any: ...


@dataclass(frozen=True)
class NexusToolBinding:
    """Bind one public MCP tool name to one Nexus operation."""

    tool: types.Tool
    service: str
    endpoint: str
    operation: str


class UnknownToolError(ValueError):
    """Report a tool name that no configured Nexus service exposed."""


TaskStatus = Literal["working", "completed", "failed", "cancelled"]


@dataclass(frozen=True)
class NexusTask:
    """Describe a durable Nexus operation as an MCP task."""

    task_id: str
    service: str
    endpoint: str
    operation: str
    status: TaskStatus
    created_at: datetime
    last_updated_at: datetime
    result: Any = None
    error: str | None = None


@runtime_checkable
class NexusTaskExecutor(Protocol):
    """Manage durable tasks for an executor that supports standalone Nexus operations."""

    async def start_task(
        self,
        *,
        service: str,
        endpoint: str,
        operation: str,
        argument: Any,
        context: RequestContext,
    ) -> NexusTask: ...

    async def get_task(self, task_id: str) -> NexusTask: ...

    async def cancel_task(self, task_id: str) -> None: ...


class NexusToolResolver:
    """List and invoke tools exposed by one or more native Nexus services.

    The resolver discovers exact routes from the configured Nexus services. It does
    not store session state. A caller can call a known tool before it lists tools.
    """

    def __init__(
        self,
        registered_servers: Mapping[str, str],
        executor: NexusOperationExecutor,
        *,
        name: str = "nexus-tools",
        allowed_servers: frozenset[str] | None = None,
    ) -> None:
        self._registered_servers = registered_servers
        self._executor = executor
        self._name = name
        self._allowed_servers = allowed_servers

    @property
    def name(self) -> str:
        return self._name

    @property
    def supports_tasks(self) -> bool:
        """Report whether this resolver can create durable MCP tasks."""
        return isinstance(self._executor, NexusTaskExecutor)

    def _servers(self) -> dict[str, str]:
        servers = dict(self._registered_servers)
        if self._allowed_servers is None:
            return servers
        return {
            name: endpoint
            for name, endpoint in servers.items()
            if name in self._allowed_servers
        }

    async def _discover_tools(
        self, context: RequestContext | None = None
    ) -> dict[str, NexusToolBinding]:
        """Fetch and validate the current exact tool bindings."""
        request_context = context or RequestContext()
        discovery_context = replace(request_context, idempotency_key=None)
        servers = self._servers()

        async def fetch(name: str, endpoint: str) -> list[NexusToolBinding]:
            result = await self._executor.execute(
                service=name,
                endpoint=endpoint,
                operation=LIST_TOOLS_OPERATION,
                argument=None,
                context=discovery_context,
            )
            raw_tools = (
                result.get("tools", [])
                if isinstance(result, dict)
                else getattr(result, "tools", [])
            )
            raw_routes = (
                result.get("routes", {})
                if isinstance(result, dict)
                else getattr(result, "routes", {})
            )
            if not isinstance(raw_routes, Mapping):
                raise ValueError(f"Service {name!r} returned invalid tool routes")

            bindings: list[NexusToolBinding] = []
            for item in raw_tools:
                tool = (
                    item
                    if isinstance(item, types.Tool)
                    else types.Tool.model_validate(item)
                )
                operation = raw_routes.get(tool.name)
                if not isinstance(operation, str) or not operation:
                    raise ValueError(
                        f"Service {name!r} did not provide a Nexus operation "
                        f"for tool {tool.name!r}"
                    )
                bindings.append(
                    NexusToolBinding(
                        tool=tool,
                        service=name,
                        endpoint=endpoint,
                        operation=operation,
                    )
                )
            return bindings

        results = await asyncio.gather(
            *(fetch(name, endpoint) for name, endpoint in servers.items())
        )
        discovered: dict[str, NexusToolBinding] = {}
        for bindings in results:
            for binding in bindings:
                if binding.tool.name in discovered:
                    raise ValueError(
                        f"More than one Nexus service exposed tool "
                        f"{binding.tool.name!r}"
                    )
                discovered[binding.tool.name] = binding
        return dict(sorted(discovered.items()))

    async def list_tools(
        self, context: RequestContext | None = None
    ) -> list[types.Tool]:
        """Fetch every configured service's tool list concurrently."""
        return [
            binding.tool for binding in (await self._discover_tools(context)).values()
        ]

    async def resolve_tool(
        self,
        tool_name: str,
        *,
        context: RequestContext | None = None,
    ) -> NexusToolBinding:
        """Resolve an exact public tool name from current service discovery."""
        route = (await self._discover_tools(context)).get(tool_name)
        if route is None:
            raise UnknownToolError(f"Tool {tool_name!r} is not registered.")
        return route

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        *,
        context: RequestContext | None = None,
    ) -> types.CallToolResult:
        """Invoke the Nexus operation bound to an exact public tool name."""
        request_context = context or RequestContext()
        route = await self.resolve_tool(tool_name, context=request_context)
        try:
            result = await self._executor.execute(
                service=route.service,
                endpoint=route.endpoint,
                operation=route.operation,
                argument=arguments or {},
                context=request_context,
            )
            return coerce_call_tool_result(result)
        except Exception as exc:  # noqa: BLE001
            return _error_result(str(exc))

    async def start_tool_task(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        *,
        context: RequestContext | None = None,
    ) -> NexusTask:
        """Start one tool call as a durable task."""
        if not isinstance(self._executor, NexusTaskExecutor):
            raise TypeError("This Nexus executor does not support tasks")
        request_context = context or RequestContext()
        route = await self.resolve_tool(tool_name, context=request_context)
        task = await self._executor.start_task(
            service=route.service,
            endpoint=route.endpoint,
            operation=route.operation,
            argument=arguments or {},
            context=request_context,
        )
        self._validate_task_route(task)
        return _normalize_task_result(task)

    async def get_task(self, task_id: str) -> NexusTask:
        """Read one durable task."""
        if not isinstance(self._executor, NexusTaskExecutor):
            raise TypeError("This Nexus executor does not support tasks")
        task = await self._executor.get_task(task_id)
        self._validate_task_route(task)
        return _normalize_task_result(task)

    async def cancel_task(self, task_id: str) -> None:
        """Request cancellation of one durable task."""
        if not isinstance(self._executor, NexusTaskExecutor):
            raise TypeError("This Nexus executor does not support tasks")
        self._validate_task_route(await self._executor.get_task(task_id))
        await self._executor.cancel_task(task_id)

    def _validate_task_route(self, task: NexusTask) -> None:
        if self._servers().get(task.service) != task.endpoint:
            raise ValueError(
                f"Task {task.task_id!r} is not registered by this resolver"
            )


def _normalize_task_result(task: NexusTask) -> NexusTask:
    if task.status != "completed":
        return task
    return replace(task, result=coerce_call_tool_result(task.result))


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        is_error=True,
    )


def coerce_call_tool_result(value: Any) -> types.CallToolResult:
    """Normalize a Nexus return value without discarding structured data."""
    if isinstance(value, types.CallToolResult):
        return value

    if isinstance(value, pydantic.BaseModel):
        value = value.model_dump(mode="json")

    if isinstance(value, dict) and "content" in value:
        try:
            return types.CallToolResult.model_validate(value)
        except pydantic.ValidationError:
            pass

    if isinstance(value, dict):
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=json.dumps(value, indent=2, sort_keys=True),
                )
            ],
            structured_content=value,
        )

    text = "" if value is None else value if isinstance(value, str) else str(value)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])
