# ABOUTME: Regression tests for RegistryServiceHandler's map-shaped Nexus fields.
#
# nex-gen 0.2.1+ wraps additionalProperties (map-shaped) fields -- remote_tools,
# arguments, result -- in a named dataclass instead of a plain dict. dataclasses don't
# validate at construction, so passing a plain dict (the old pydantic-era shape) builds
# fine and only crashes once the real Nexus wire serializes it:
#   AttributeError: 'dict' object has no attribute 'additional_properties'
# These call the real handler methods and round-trip their return values through the
# real payload converter -- the exact step that raised -- to catch that failure mode.
#
# temporalio.nexus.info() only works inside a real dispatched Nexus operation; it's
# patched here (a public SDK entry point, not an internal) rather than standing up a
# live worker + real MCP server, which this environment's asyncio/Temporal interaction
# made too flaky to rely on.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import temporalio.nexus
from mcp.types import CallToolResult, TextContent
from nexus_mcp.durable_tools_gateway.generated import (
    CallToolInput,
    CallToolInputArguments,
    CallToolOutput,
    ListAccountEntriesInput,
    ListAccountEntriesOutput,
)
from nexus_mcp.durable_tools_gateway.registry import (
    AccountEntries,
    ToolRegistryWorkflow,
    account_registry_workflow_id,
    fetch_external_tools,
)
from nexus_mcp.durable_tools_gateway.registry_service_handler import (
    RegistryServiceHandler,
    mcp_proxy_activity,
)
from temporalio.converter import DataConverter

_payload_converter = DataConverter.default.payload_converter
_FAKE_NEXUS_INFO = temporalio.nexus.Info(
    endpoint="test-endpoint", namespace="test-namespace", task_queue="test-task-queue"
)


def _round_trip(value: object, type_hint: type) -> object:
    [payload] = _payload_converter.to_payloads([value])
    [decoded] = _payload_converter.from_payloads([payload], [type_hint])
    return decoded


def _mock_client(*, remote_servers=None, fetched_tools=None, find_url=None, call_result=None):
    """A fake temporalio.client.Client just capable enough for RegistryServiceHandler."""
    client = MagicMock()
    handle = MagicMock()

    async def query(method, *args, **kwargs):
        if method is ToolRegistryWorkflow.list_account_entries:
            return AccountEntries(remote_servers=remote_servers or {})
        if method is ToolRegistryWorkflow.find:
            return find_url
        raise AssertionError(f"unexpected query: {method}")

    handle.query = AsyncMock(side_effect=query)
    client.start_workflow = AsyncMock(return_value=handle)

    async def execute_activity(activity, *args, **kwargs):
        if activity is fetch_external_tools:
            return fetched_tools or []
        if activity is mcp_proxy_activity:
            return call_result
        raise AssertionError(f"unexpected activity: {activity}")

    client.execute_activity = AsyncMock(side_effect=execute_activity)
    return client


@patch("temporalio.nexus.info", return_value=_FAKE_NEXUS_INFO)
async def test_list_account_entries_output_serializes_over_the_wire(_mock_info: MagicMock) -> None:
    client = _mock_client(
        remote_servers={"weather": "http://fake"},
        fetched_tools=[{"name": "weather_get_forecast", "description": "fake"}],
    )
    handler = RegistryServiceHandler(client)

    output = await handler.list_account_entries(
        MagicMock(), ListAccountEntriesInput(account_id="account-1")
    )

    decoded = _round_trip(output, ListAccountEntriesOutput)
    tools = decoded.remote_tools.additional_properties["weather"]
    assert tools[0].additional_properties["name"] == "weather_get_forecast"
    options = client.execute_activity.await_args.kwargs
    assert options["schedule_to_close_timeout"].total_seconds() == 60
    assert options["retry_policy"].maximum_attempts == 3
    assert client.start_workflow.await_args.kwargs["id"] == account_registry_workflow_id(
        "account-1"
    )


@patch("temporalio.nexus.info", return_value=_FAKE_NEXUS_INFO)
async def test_call_tool_forwards_arguments_and_serializes_result(_mock_info: MagicMock) -> None:
    fake_result = CallToolResult(content=[TextContent(type="text", text="42")])
    client = _mock_client(find_url="http://fake", call_result=fake_result)
    handler = RegistryServiceHandler(client)

    output = await handler.call_tool(
        MagicMock(),
        CallToolInput(
            account_id="account-1",
            alias="weather",
            name="weather_get_forecast",
            arguments=CallToolInputArguments(additional_properties={"city": "NYC"}),
        ),
    )

    # Proves the wrapper got unwrapped before reaching the (mocked) activity, not just
    # that construction didn't crash.
    activity_input = client.execute_activity.await_args.args[1]
    assert activity_input.arguments == {"city": "NYC"}

    decoded = _round_trip(output, CallToolOutput)
    assert decoded.result.additional_properties["content"][0]["text"] == "42"
