from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from nexus_mcp.durable_tools_gateway.registry import (
    AgentRegistration,
    NexusMCPServerRegistration,
    SessionRecord,
)
from nexus_mcp.durable_tools_gateway.registry_service_handler import ExternalMCPCallInput
from nexus_mcp.durable_tools_gateway.tool_history import (
    _describe_external_call,
    _native_calls_from_history,
    scan_native_tool_calls,
)
from mcp.types import CallToolResult
from temporalio.api.history.v1 import HistoryEvent
from temporalio.api.operatorservice.v1 import ListNexusEndpointsResponse
from temporalio.api.workflowservice.v1 import (
    DescribeActivityExecutionResponse,
    GetWorkflowExecutionHistoryResponse,
)
from temporalio.contrib.pydantic import pydantic_data_converter


async def test_native_nexus_calls_are_correlated_from_workflow_history() -> None:
    scheduled = HistoryEvent(event_id=12)
    scheduled.event_time.FromDatetime(
        datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    )
    scheduled_attributes = scheduled.nexus_operation_scheduled_event_attributes
    scheduled_attributes.endpoint = "native-demo-endpoint"
    scheduled_attributes.service = "demo-nexus"
    scheduled_attributes.operation = "get_lucky_number"
    scheduled_attributes.request_id = "request-1"
    [input_payload] = await pydantic_data_converter.encode(
        [{"topic": "Temporal"}]
    )
    scheduled_attributes.input.CopyFrom(input_payload)

    started = HistoryEvent(event_id=14)
    started_attributes = started.nexus_operation_started_event_attributes
    started_attributes.scheduled_event_id = 12
    started_attributes.operation_id = "operation-1"

    completed = HistoryEvent(event_id=15)
    completed.event_time.FromDatetime(
        datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC)
    )
    completed_attributes = completed.nexus_operation_completed_event_attributes
    completed_attributes.scheduled_event_id = 12
    [result_payload] = await pydantic_data_converter.encode([{"value": 7}])
    completed_attributes.result.CopyFrom(result_payload)

    response = GetWorkflowExecutionHistoryResponse()
    response.history.events.extend([scheduled, started, completed])
    client = MagicMock()
    client.data_converter = pydantic_data_converter
    client.workflow_service.get_workflow_execution_history = AsyncMock(
        return_value=response
    )

    calls = await _native_calls_from_history(
        client,
        server=NexusMCPServerRegistration(
            name="native-demo",
            endpoint="native-demo-endpoint",
            service="demo-nexus",
        ),
        namespace="default",
        workflow_id="session-1",
        agent_id="assistant",
    )

    assert len(calls) == 1
    assert calls[0].call_id == "nexus:default:session-1:12"
    assert calls[0].tool_name == "get_lucky_number"
    assert calls[0].status == "completed"
    assert calls[0].duration_ms == 1000
    assert calls[0].nexus_request_id == "request-1"
    assert calls[0].nexus_operation_id == "operation-1"
    assert calls[0].input == {"topic": "Temporal"}
    assert calls[0].output == {"value": 7}


async def test_external_mcp_call_is_hydrated_from_retained_saa() -> None:
    response = DescribeActivityExecutionResponse()
    response.info.activity_id = "mcp-proxy-1"
    response.info.activity_type.name = "mcp_proxy_activity"
    response.info.task_queue = "mcp-registry"
    response.info.schedule_time.FromDatetime(
        datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    )
    response.info.close_time.FromDatetime(
        datetime(2026, 9, 1, 12, 0, 2, tzinfo=UTC)
    )
    activity_input = ExternalMCPCallInput(
        account_id="account-1",
        server_name="weather",
        caller_workflow_id="session-1",
        server_url="http://weather",
        tool_name="forecast",
        arguments={"city": "New York"},
    )
    activity_output = CallToolResult(content=[], isError=False)
    response.input.payloads.extend(
        await pydantic_data_converter.encode([activity_input])
    )
    response.outcome.result.payloads.extend(
        await pydantic_data_converter.encode([activity_output])
    )
    client = MagicMock()
    client.namespace = "gateway"
    client.data_converter = pydantic_data_converter
    client.workflow_service.describe_activity_execution = AsyncMock(
        return_value=response
    )

    described = await _describe_external_call(client, "mcp-proxy-1")

    assert described is not None
    decoded_input, call = described
    assert decoded_input.account_id == "account-1"
    assert call.call_id == "activity:gateway:mcp-proxy-1"
    assert call.transport == "external_http"
    assert call.workflow_id == "session-1"
    assert call.tool_name == "forecast"
    assert call.status == "completed"
    assert call.duration_ms == 2000
    assert call.input == {"city": "New York"}


async def test_native_scan_resolves_only_account_session_workflow_namespaces() -> None:
    endpoints = ListNexusEndpointsResponse()
    endpoint = endpoints.endpoints.add()
    endpoint.spec.name = "assistant-endpoint"
    endpoint.spec.target.worker.namespace = "agent-namespace"
    endpoint.spec.target.worker.task_queue = "assistant"
    client = MagicMock()
    client.operator_service.list_nexus_endpoints = AsyncMock(
        return_value=endpoints
    )
    server = NexusMCPServerRegistration(
        name="native-demo",
        endpoint="native-demo-endpoint",
        service="demo-nexus",
    )
    agent = AgentRegistration(
        agent_id="assistant",
        kind="harness_nexus",
        label="Assistant",
        description="Test agent",
        nexus_endpoint="assistant-endpoint",
    )
    session = SessionRecord(
        account_id="account-1",
        session_id="session-1",
        agent_id="assistant",
        provider_session_id="provider-workflow-1",
        created_at=123.0,
        label="Session 1",
        has_started=True,
    )

    with patch(
        "nexus_mcp.durable_tools_gateway.tool_history._native_calls_from_history",
        new=AsyncMock(return_value=[]),
    ) as read_history:
        calls = await scan_native_tool_calls(
            client,
            server=server,
            agents=[agent],
            sessions=[session],
        )

    assert calls == []
    assert read_history.await_args.kwargs["namespace"] == "agent-namespace"
    assert read_history.await_args.kwargs["workflow_id"] == "provider-workflow-1"
