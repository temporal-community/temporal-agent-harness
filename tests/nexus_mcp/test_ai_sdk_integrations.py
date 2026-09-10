"""Test the in-process AI SDK integrations."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from agents import Agent as OpenAIAgent
from agents import Runner
from mcp import types
from nexus_mcp.integrations.openai_agents import (
    WorkflowNexusMCPServer,
    _ResolverBackedMCPServer,
)
from nexus_mcp.integrations.pydantic_ai import ResolverBackedToolset
from nexus_mcp.resolver import NexusToolResolver, RequestContext
from pydantic_ai import Agent as PydanticAIAgent
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel as PydanticAITestModel

from temporal_agent_harness.ai_sdks.openai_agents.testing import (
    ResponseBuilders,
    TestModel,
)
from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
    nexus_native_mcp_server,
)


class _Executor:
    def __init__(self, result: Any = None) -> None:
        self.contexts: list[RequestContext] = []
        self.result = result if result is not None else {"answer": 42}

    async def execute(self, **kwargs: Any) -> Any:
        self.contexts.append(kwargs["context"])
        if kwargs["operation"] == "list_tools":
            return {
                "tools": [
                    {
                        "name": "weather_forecast",
                        "description": "Get a forecast.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                        "outputSchema": {
                            "type": "object",
                            "properties": {"answer": {"type": "integer"}},
                        },
                        "annotations": {"readOnlyHint": True},
                        "_meta": {"source": "test"},
                    }
                ],
                "routes": {"weather_forecast": "forecast"},
            }
        return self.result


def _resolver(executor: _Executor) -> NexusToolResolver:
    return NexusToolResolver(
        {"weather": "weather-endpoint"},
        executor,
        name="weather",
    )


async def test_openai_adapter_lists_and_calls_tools() -> None:
    executor = _Executor()
    server = _ResolverBackedMCPServer(_resolver(executor))

    tools = await server.list_tools()
    result = await server.call_tool(
        "weather_forecast",
        {"city": "Boston"},
        meta={"io.temporal/idempotencyKey": "forecast-1"},
    )

    assert [tool.name for tool in tools] == ["weather_forecast"]
    assert result.structured_content == {"answer": 42}
    assert executor.contexts[-1].idempotency_key == "forecast-1"


async def test_openai_adapter_returns_an_unknown_tool_error() -> None:
    server = _ResolverBackedMCPServer(_resolver(_Executor()))

    result = await server.call_tool("missing", {})

    assert result.is_error is True
    assert result.content[0].text == "Tool 'missing' is not registered."


def test_workflow_openai_adapter_has_a_distinct_type() -> None:
    server = WorkflowNexusMCPServer.for_service("weather", "weather-endpoint")

    assert isinstance(server, _ResolverBackedMCPServer)
    assert server.name == "weather"


def test_agent_harness_factory_uses_the_shared_workflow_adapter() -> None:
    server = nexus_native_mcp_server("weather", "weather-endpoint")

    assert isinstance(server, WorkflowNexusMCPServer)


async def test_openai_agent_uses_the_server_in_process() -> None:
    server = _ResolverBackedMCPServer(_resolver(_Executor()))
    model = TestModel.returning_responses(
        [
            ResponseBuilders.tool_call(
                '{"city":"Boston"}',
                "weather_forecast",
            ),
            ResponseBuilders.output_message("42"),
        ]
    )
    agent = OpenAIAgent(
        name="weather-agent",
        model=model,
        mcp_servers=[server],
    )

    result = await Runner.run(agent, "Get the forecast.")

    assert result.final_output == "42"


async def test_pydantic_ai_toolset_lists_and_calls_tools() -> None:
    executor = _Executor()
    toolset = ResolverBackedToolset(_resolver(executor), include_return_schema=True)
    ctx = MagicMock(max_retries=3)

    tools = await toolset.get_tools(ctx)
    tool = tools["weather_forecast"]
    result = await toolset.call_tool(
        "weather_forecast",
        {"city": "Boston"},
        ctx,
        tool,
    )

    assert tool.max_retries == 3
    assert tool.tool_def.parameters_json_schema["required"] == ["city"]
    assert tool.tool_def.return_schema is not None
    assert tool.tool_def.metadata == {
        "meta": {"source": "test"},
        "annotations": {"readOnlyHint": True},
    }
    assert result == {"answer": 42}


async def test_pydantic_ai_toolset_maps_tool_errors_to_model_retry() -> None:
    error = types.CallToolResult(
        content=[types.TextContent(type="text", text="Forecast failed.")],
        is_error=True,
    )
    toolset = ResolverBackedToolset(_resolver(_Executor(error)))
    ctx = MagicMock(max_retries=1)
    tool = (await toolset.get_tools(ctx))["weather_forecast"]

    with pytest.raises(ModelRetry, match="Forecast failed"):
        await toolset.call_tool("weather_forecast", {}, ctx, tool)


async def test_pydantic_ai_toolset_accepts_an_async_context_factory() -> None:
    executor = _Executor()

    async def context_factory(
        ctx: Any,
        name: str | None,
        arguments: dict[str, Any] | None,
    ) -> RequestContext:
        if name is None or arguments is None:
            return RequestContext(nexus_headers={"authorization": "test"})
        return RequestContext(idempotency_key=f"{name}-{arguments['city']}")

    toolset = ResolverBackedToolset(
        _resolver(executor),
        request_context_factory=context_factory,
    )
    ctx = MagicMock(max_retries=1)
    tool = (await toolset.get_tools(ctx))["weather_forecast"]

    await toolset.call_tool(
        "weather_forecast",
        {"city": "Boston"},
        ctx,
        tool,
    )

    assert executor.contexts[-1].idempotency_key == "weather_forecast-Boston"
    assert executor.contexts[0].nexus_headers == {"authorization": "test"}


async def test_pydantic_ai_agent_uses_the_toolset_in_process() -> None:
    toolset = ResolverBackedToolset(_resolver(_Executor()))
    agent = PydanticAIAgent(
        PydanticAITestModel(call_tools=["weather_forecast"]),
        toolsets=[toolset],
    )

    result = await agent.run("Get the forecast.")

    assert result.output == '{"weather_forecast":{"answer":42}}'
