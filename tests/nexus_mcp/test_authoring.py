# ABOUTME: Tests for authoring MCP tools as native Nexus service operations.

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import nexusrpc.handler
import temporalio.nexus
from mcp.types import ToolAnnotations
from nexus_mcp.authoring import (
    MCPOverNexusServiceHandler,
    MCPToolConfig,
    build_tool_dicts,
    build_tool_routes,
    nexus_mcp_operation,
    nexus_mcp_tool,
)
from nexusrpc.handler import OperationTaskCancellation, StartOperationContext
from pydantic import BaseModel


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
    @nexusrpc.handler.sync_operation(name="modeled-forecast")
    async def modeled(
        self,
        ctx: StartOperationContext,
        input: _WeatherInput,
    ) -> _WeatherOutput:
        """Use explicit Nexus input and output models."""
        return _WeatherOutput(summary=input.city)

    @nexus_mcp_operation
    @temporalio.nexus.workflow_run_operation
    async def delayed(
        self,
        ctx: temporalio.nexus.WorkflowRunOperationContext,
        input: _WeatherInput,
    ) -> temporalio.nexus.WorkflowHandle[_WeatherOutput]:
        """Start a workflow-backed tool."""
        raise NotImplementedError

    @nexusrpc.handler.sync_operation
    async def internal_operation(
        self,
        ctx: StartOperationContext,
        input: str,
    ) -> None:
        """Do not expose this operation as an MCP tool."""
        return


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
    output = await _WeatherService().list_tools(
        StartOperationContext(
            service="weather",
            operation="list_tools",
            headers={},
            task_cancellation=MagicMock(spec=OperationTaskCancellation),
            request_id="request-1",
        ),
        None,
    )

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
        StartOperationContext(
            service="weather",
            operation="conditions",
            headers={},
            task_cancellation=MagicMock(spec=OperationTaskCancellation),
            request_id="request-2",
        ),
        cast(Any, operation.input_type)(city="New York"),
    )

    assert output == {"city": "New York"}
