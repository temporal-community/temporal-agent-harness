# ABOUTME: Regression tests for _NexusGatewayMCPServer's map-shaped Nexus fields.
#
# nex-gen 0.2.1+ wraps additionalProperties (map-shaped) fields -- remote_tools,
# arguments, result -- in a named dataclass instead of a plain dict. This is the
# client-side counterpart to RegistryServiceHandler's own bugs (see
# tests/nexus_mcp/test_registry_wire_shapes.py): reading a wrapper as a plain dict
# fails with e.g. "'ListAccountEntriesOutputRemoteTools' object has no attribute 'get'",
# and constructing a wrapper-typed field from a plain dict crashes on the real wire.
#
# workflow.create_nexus_client() only works inside a real workflow, so it's mocked here
# rather than exercised over a live server.

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from durable_tools_gateway.generated import (
    CallToolOutput,
    CallToolOutputResult,
    ListAccountEntriesOutput,
    ListAccountEntriesOutputRemoteTools,
    ListAccountEntriesOutputRemoteToolsValueItem,
)
from durable_tools_gateway.resources import ResourceDescriptor, text_agent_card
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp import (
    _materialize_toolbox,
    _NexusGatewayMCPServer,
)
from tests.ai_sdks.openai_agents._toolbox_sandbox_probe import ToolboxSandboxProbe


def _server() -> _NexusGatewayMCPServer:
    return _NexusGatewayMCPServer(
        account_id="agent-1",
        aliases=frozenset({"weather"}),
        gateway_name="RegistryService",
        gateway_endpoint="mcp-registry-endpoint",
        display_name="agent-1-RegistryService-mcp-registry-endpoint",
    )


def test_dynamic_toolbox_materializes_native_and_external_routes() -> None:
    toolbox = _materialize_toolbox(
        [
            ResourceDescriptor(
                "research",
                1,
                "agent",
                "nexus",
                "Research",
                "",
                "research-endpoint",
                "A2AService",
                text_agent_card(
                    name="Research",
                    description="",
                    endpoint="research-endpoint",
                    transport="nexus",
                ),
            ),
            ResourceDescriptor(
                "writer",
                1,
                "agent",
                "external_http",
                "Writer",
                "",
                "http://writer",
                agent_card=text_agent_card(
                    name="Writer",
                    description="",
                    endpoint="http://writer",
                    transport="external_http",
                ),
            ),
            ResourceDescriptor(
                "native-tools",
                1,
                "mcp",
                "nexus",
                "Native tools",
                "",
                "tools-endpoint",
                "tools-service",
            ),
            ResourceDescriptor(
                "remote-tools",
                1,
                "mcp",
                "external_http",
                "Remote tools",
                "",
                "http://tools/mcp",
            ),
        ],
        account_id="account-1",
        gateway_name="RegistryService",
        gateway_endpoint="registry-endpoint",
        version="abc",
    )

    assert toolbox.version == "abc"
    assert [server.name for server in toolbox.mcp_servers] == [
        "tools-service",
        "account-1-RegistryService-registry-endpoint",
    ]
    assert [tool.__name__ for tool in toolbox.subagent_tools] == [
        "start_research",
        "research_ask",
        "stop_research",
        "start_writer",
        "writer_ask",
        "stop_writer",
    ]


async def test_dynamic_toolbox_materializes_inside_workflow_sandbox() -> None:
    env = await WorkflowEnvironment.start_time_skipping()
    task_queue = f"toolbox-sandbox-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[ToolboxSandboxProbe],
    ):
        try:
            names = await env.client.execute_workflow(
                ToolboxSandboxProbe.run,
                id=f"toolbox-sandbox-{uuid.uuid4()}",
                task_queue=task_queue,
            )
        finally:
            await env.shutdown()

    assert names == [
        "start_research",
        "research_ask",
        "stop_research",
    ]


@patch(
    "temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp.workflow.create_nexus_client"
)
async def test_list_tools_unwraps_remote_tools(mock_create_client: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.execute_operation = AsyncMock(
        return_value=ListAccountEntriesOutput(
            remote_tools=ListAccountEntriesOutputRemoteTools(
                additional_properties={
                    "weather": [
                        ListAccountEntriesOutputRemoteToolsValueItem(
                            additional_properties={
                                "name": "weather_get_forecast",
                                "description": "fake",
                                "inputSchema": {"type": "object"},
                            }
                        )
                    ]
                }
            )
        )
    )
    mock_create_client.return_value = mock_client
    server = _server()

    tools = await server.list_tools()

    assert [t.name for t in tools] == ["weather_get_forecast"]
    assert server._remote_routes == {"weather_get_forecast": "weather"}


@patch(
    "temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp.workflow.create_nexus_client"
)
@patch("temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp.workflow.info")
async def test_call_tool_wraps_arguments_and_unwraps_result(
    mock_info: MagicMock, mock_create_client: MagicMock
) -> None:
    mock_info.return_value = SimpleNamespace(workflow_id="workflow-1")
    mock_client = MagicMock()
    mock_client.execute_operation = AsyncMock(
        return_value=CallToolOutput(
            result=CallToolOutputResult(
                additional_properties={
                    "content": [{"type": "text", "text": "42"}],
                    "isError": False,
                }
            )
        )
    )
    mock_create_client.return_value = mock_client
    server = _server()
    server._remote_routes = {"weather_get_forecast": "weather"}

    result = await server.call_tool("weather_get_forecast", {"city": "NYC"})

    call_tool_input = mock_client.execute_operation.await_args.args[1]
    assert call_tool_input.caller_workflow_id == "workflow-1"
    assert call_tool_input.arguments.additional_properties == {"city": "NYC"}
    assert result.content[0].text == "42"
