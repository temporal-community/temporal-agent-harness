# ABOUTME: Tests for authoring MCP tools as native Nexus service operations.

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import nexusrpc.handler
from mcp.types import ToolAnnotations
from nexus_mcp.authoring import (
    MCPOverNexusServiceHandler,
    MCPToolConfig,
    build_tool_dicts,
    build_tool_routes,
    nexus_mcp_operation,
    nexus_mcp_tool,
)
from nexusrpc.handler import OperationTaskCancellation
from pydantic import BaseModel
from temporalio import nexus


class _WeatherInput(BaseModel):
    city: str


class _WeatherOutput(BaseModel):
    summary: str


@nexusrpc.handler.service_handler(name="weather")
class _WeatherService(MCPOverNexusServiceHandler):
    mcp_tool_defaults = MCPToolConfig(
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        meta={"owner": "weather-team"},
    )

    @nexus_mcp_tool(
        name="predict",
        title="Forecast",
        description="Return the configured forecast.",
        annotations=ToolAnnotations(idempotent_hint=True),
        meta={"audience": "public"},
    )
    async def forecast(self, city: str, days: int = 3) -> dict[str, object]:
        """Get a weather forecast."""
        return {"city": city, "days": days}

    @nexus_mcp_tool
    def conditions(self, city: str) -> dict[str, str]:
        """Get current weather conditions."""
        return {"city": city}

    @nexus_mcp_operation(title="Modeled forecast")
    @nexus.temporal_operation(name="modeled-forecast")
    async def modeled(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: _WeatherInput,
    ) -> nexus.TemporalOperationResult[_WeatherOutput]:
        """Use explicit Nexus input and output models."""
        return nexus.TemporalOperationResult.sync(_WeatherOutput(summary=input.city))

    @nexus_mcp_operation
    @nexus.temporal_operation
    async def delayed(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: _WeatherInput,
    ) -> nexus.TemporalOperationResult[_WeatherOutput]:
        """Start a workflow-backed tool."""
        raise NotImplementedError

    @nexus.temporal_operation
    async def internal_operation(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: str,
    ) -> nexus.TemporalOperationResult[None]:
        """Do not expose this operation as an MCP tool."""
        return nexus.TemporalOperationResult.sync(None)


def test_build_tool_dicts_exposes_only_decorated_operations() -> None:
    tools = build_tool_dicts(_WeatherService)

    assert len(tools) == 4
    tool = next(tool for tool in tools if tool["name"] == "weather_predict")
    assert tool["name"] == "weather_predict"
    assert tool["title"] == "Forecast"
    assert tool["description"] == "Return the configured forecast."
    assert tool["inputSchema"]["required"] == ["city"]
    assert tool["inputSchema"]["properties"]["days"]["default"] == 3
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["idempotentHint"] is True
    assert tool["annotations"]["openWorldHint"] is False
    assert tool["_meta"] == {
        "owner": "weather-team",
        "audience": "public",
    }

    modeled = next(tool for tool in tools if tool["name"] == "weather_modeled-forecast")
    assert modeled["title"] == "Modeled forecast"
    assert modeled["inputSchema"]["properties"]["city"]["type"] == "string"
    assert modeled["outputSchema"]["properties"]["summary"]["type"] == "string"

    delayed = next(tool for tool in tools if tool["name"] == "weather_delayed")
    assert delayed["outputSchema"]["required"] == ["summary"]

    assert build_tool_routes(_WeatherService) == {
        "weather_conditions": "conditions",
        "weather_delayed": "delayed",
        "weather_modeled-forecast": "modeled-forecast",
        "weather_predict": "predict",
    }


async def test_service_list_tools_uses_the_runtime_service_name() -> None:
    result = await _WeatherService().list_tools(
        nexus.TemporalStartOperationContext(
            service="weather",
            operation="list_tools",
            headers={},
            task_cancellation=MagicMock(spec=OperationTaskCancellation),
            request_id="request-1",
        ),
        MagicMock(spec=nexus.TemporalNexusClient),
        None,
    )
    output = result.value

    assert sorted(tool["name"] for tool in output.tools) == [
        "weather_conditions",
        "weather_delayed",
        "weather_modeled-forecast",
        "weather_predict",
    ]
    assert output.routes["weather_predict"] == "predict"


async def test_convenience_decorator_accepts_sync_methods() -> None:
    operation = nexusrpc.get_operation(_WeatherService.conditions)
    assert operation is not None

    output = await cast(Any, _WeatherService().conditions)(
        nexus.TemporalStartOperationContext(
            service="weather",
            operation="conditions",
            headers={},
            task_cancellation=MagicMock(spec=OperationTaskCancellation),
            request_id="request-2",
        ),
        MagicMock(spec=nexus.TemporalNexusClient),
        cast(Any, operation.input_type)(city="New York"),
    )

    assert output.value == {"city": "New York"}
