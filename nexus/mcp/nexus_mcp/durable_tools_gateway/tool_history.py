"""Read-only reconstruction of account MCP calls from Temporal retention."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, is_dataclass
from typing import Any

from nexus_mcp.authoring import LIST_TOOLS_OPERATION
from mcp.types import CallToolResult
from pydantic import BaseModel
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.operatorservice.v1 import ListNexusEndpointsRequest
from temporalio.api.workflowservice.v1 import (
    DescribeActivityExecutionRequest,
    GetWorkflowExecutionHistoryRequest,
)
from temporalio.client import Client
from temporalio.converter import (
    ActivitySerializationContext,
    WorkflowSerializationContext,
)
from temporalio.service import RPCError, RPCStatusCode

from .registry import (
    AgentRegistration,
    NexusMCPServerRegistration,
    SessionRecord,
)
from .registry_service_handler import ExternalMCPCallInput, mcp_proxy_activity

_READ_CONCURRENCY = 8
_MAX_ACTIVITY_SCAN = 500
logger = logging.getLogger(__name__)


class ToolCallRecord(BaseModel):
    """One retained call projected into the account owner's inspector."""

    call_id: str
    server_name: str
    transport: str
    tool_name: str
    status: str
    scheduled_at: float
    completed_at: float | None = None
    duration_ms: float | None = None
    namespace: str
    execution_id: str
    workflow_id: str | None = None
    agent_id: str | None = None
    nexus_request_id: str | None = None
    nexus_operation_id: str | None = None
    input: Any = None
    output: Any = None
    error: str | None = None


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _json_safe(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


async def _decode_payload(converter: Any, payload: Any) -> Any:
    [value] = await converter.decode([payload], [Any])
    return _json_safe(value)


async def _endpoint_namespaces(client: Client) -> dict[str, str]:
    """Resolve Nexus endpoint names once; worker targets reveal agent namespaces."""
    result: dict[str, str] = {}
    token = b""
    while True:
        response = await client.operator_service.list_nexus_endpoints(
            ListNexusEndpointsRequest(page_size=1000, next_page_token=token),
            retry=True,
        )
        for endpoint in response.endpoints:
            target = endpoint.spec.target
            if target.HasField("worker"):
                result[endpoint.spec.name] = target.worker.namespace
        token = bytes(response.next_page_token)
        if not token:
            return result


async def _workflow_history(
    client: Client, namespace: str, workflow_id: str
) -> list[Any]:
    events: list[Any] = []
    token = b""
    while True:
        try:
            response = await client.workflow_service.get_workflow_execution_history(
                GetWorkflowExecutionHistoryRequest(
                    namespace=namespace,
                    execution=WorkflowExecution(workflow_id=workflow_id),
                    maximum_page_size=1000,
                    next_page_token=token,
                    skip_archival=False,
                ),
                retry=True,
            )
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return []
            raise
        events.extend(response.history.events)
        token = bytes(response.next_page_token)
        if not token:
            return events


async def _native_calls_from_history(
    client: Client,
    *,
    server: NexusMCPServerRegistration,
    namespace: str,
    workflow_id: str,
    agent_id: str,
) -> list[ToolCallRecord]:
    events = await _workflow_history(client, namespace, workflow_id)
    scheduled: dict[int, Any] = {}
    started: dict[int, Any] = {}
    terminal: dict[int, tuple[str, Any, Any]] = {}
    for event in events:
        if event.HasField("nexus_operation_scheduled_event_attributes"):
            attributes = event.nexus_operation_scheduled_event_attributes
            if (
                attributes.endpoint == server.endpoint
                and attributes.service == server.service
                and attributes.operation != LIST_TOOLS_OPERATION
            ):
                scheduled[event.event_id] = event
            continue
        if event.HasField("nexus_operation_started_event_attributes"):
            attributes = event.nexus_operation_started_event_attributes
            started[attributes.scheduled_event_id] = attributes
            continue
        for field_name, status in (
            ("nexus_operation_completed_event_attributes", "completed"),
            ("nexus_operation_failed_event_attributes", "failed"),
            ("nexus_operation_canceled_event_attributes", "canceled"),
            ("nexus_operation_timed_out_event_attributes", "timed_out"),
        ):
            if event.HasField(field_name):
                attributes = getattr(event, field_name)
                terminal[attributes.scheduled_event_id] = (
                    status,
                    event,
                    attributes,
                )
                break

    converter = client.data_converter.with_context(
        WorkflowSerializationContext(namespace=namespace, workflow_id=workflow_id)
    )
    calls: list[ToolCallRecord] = []
    for event_id, event in scheduled.items():
        attributes = event.nexus_operation_scheduled_event_attributes
        call_input = await _decode_payload(converter, attributes.input)
        scheduled_at = event.event_time.ToDatetime().timestamp()
        status = "running"
        completed_at = None
        output = None
        error = None
        finished = terminal.get(event_id)
        if finished is not None:
            status, terminal_event, terminal_attributes = finished
            completed_at = terminal_event.event_time.ToDatetime().timestamp()
            if status == "completed":
                output = await _decode_payload(converter, terminal_attributes.result)
            else:
                failure = await converter.decode_failure(terminal_attributes.failure)
                error = str(failure)
        calls.append(
            ToolCallRecord(
                call_id=f"nexus:{namespace}:{workflow_id}:{event_id}",
                server_name=server.name,
                transport="nexus",
                tool_name=attributes.operation,
                status=status,
                scheduled_at=scheduled_at,
                completed_at=completed_at,
                duration_ms=(
                    round((completed_at - scheduled_at) * 1000, 3)
                    if completed_at is not None
                    else None
                ),
                namespace=namespace,
                execution_id=str(event_id),
                workflow_id=workflow_id,
                agent_id=agent_id,
                nexus_request_id=attributes.request_id or None,
                nexus_operation_id=(
                    started[event_id].operation_id or None
                    if event_id in started
                    else None
                ),
                input=call_input,
                output=output,
                error=error,
            )
        )
    return calls


async def scan_native_tool_calls(
    client: Client,
    *,
    server: NexusMCPServerRegistration,
    agents: list[AgentRegistration],
    sessions: list[SessionRecord],
) -> list[ToolCallRecord]:
    """Scan only account-known harness workflows, never namespace Visibility."""
    endpoint_namespaces = await _endpoint_namespaces(client)
    agents_by_id = {agent.agent_id: agent for agent in agents}
    workflows: dict[tuple[str, str], str] = {}
    for session in sessions:
        agent = agents_by_id.get(session.agent_id)
        if (
            not session.has_started
            or agent is None
            or agent.kind != "harness_nexus"
            or not agent.nexus_endpoint
        ):
            continue
        namespace = endpoint_namespaces.get(agent.nexus_endpoint)
        if namespace:
            workflows[(namespace, session.provider_session_id)] = agent.agent_id

    limit = asyncio.Semaphore(_READ_CONCURRENCY)

    async def scan(
        namespace: str, workflow_id: str, agent_id: str
    ) -> list[ToolCallRecord]:
        try:
            async with limit:
                return await _native_calls_from_history(
                    client,
                    server=server,
                    namespace=namespace,
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                )
        except Exception:
            logger.warning(
                "Could not inspect MCP history for %s/%s",
                namespace,
                workflow_id,
                exc_info=True,
            )
            return []

    batches = await asyncio.gather(
        *(
            scan(namespace, workflow_id, agent_id)
            for (namespace, workflow_id), agent_id in workflows.items()
        )
    )
    return sorted(
        (call for batch in batches for call in batch),
        key=lambda call: call.scheduled_at,
        reverse=True,
    )


async def _describe_external_call(
    client: Client, activity_id: str
) -> tuple[ExternalMCPCallInput, ToolCallRecord] | None:
    response = await client.workflow_service.describe_activity_execution(
        DescribeActivityExecutionRequest(
            namespace=client.namespace,
            activity_id=activity_id,
            include_input=True,
            include_outcome=True,
        ),
        retry=True,
    )
    if not response.input.payloads:
        return None
    info = response.info
    converter = client.data_converter.with_context(
        ActivitySerializationContext(
            namespace=client.namespace,
            activity_id=activity_id,
            activity_type=info.activity_type.name,
            activity_task_queue=info.task_queue,
            workflow_id=None,
            workflow_type=None,
            is_local=False,
        )
    )
    [activity_input] = await converter.decode(
        response.input.payloads, [ExternalMCPCallInput]
    )
    scheduled_at = info.schedule_time.ToDatetime().timestamp()
    completed_at = (
        info.close_time.ToDatetime().timestamp()
        if info.HasField("close_time")
        else None
    )
    status = "running"
    output = None
    error = None
    if response.HasField("outcome"):
        if response.outcome.HasField("failure"):
            status = "failed"
            failure = await converter.decode_failure(response.outcome.failure)
            error = str(failure)
        elif response.outcome.result.payloads:
            [result] = await converter.decode(
                response.outcome.result.payloads, [CallToolResult]
            )
            status = "failed" if result.is_error else "completed"
            output = _json_safe(result)
    return (
        activity_input,
        ToolCallRecord(
            call_id=f"activity:{client.namespace}:{activity_id}",
            server_name=activity_input.server_name,
            transport="external_http",
            tool_name=activity_input.tool_name,
            status=status,
            scheduled_at=scheduled_at,
            completed_at=completed_at,
            duration_ms=(
                round((completed_at - scheduled_at) * 1000, 3)
                if completed_at is not None
                else None
            ),
            namespace=client.namespace,
            execution_id=activity_id,
            workflow_id=activity_input.caller_workflow_id,
            input=activity_input.arguments,
            output=output,
            error=error,
        ),
    )


async def scan_external_tool_calls(
    client: Client, *, account_id: str, server_name: str
) -> list[ToolCallRecord]:
    """Scan retained gateway SAAs and hydrate only this account's calls."""
    executions = [
        execution
        async for execution in client.list_activities(
            f'ActivityType = "{mcp_proxy_activity.__name__}"',
            limit=_MAX_ACTIVITY_SCAN,
        )
    ]
    limit = asyncio.Semaphore(_READ_CONCURRENCY)

    async def describe(activity_id: str) -> ToolCallRecord | None:
        try:
            async with limit:
                described = await _describe_external_call(client, activity_id)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return None
            logger.warning(
                "Could not inspect retained MCP activity %s: %s",
                activity_id,
                exc,
            )
            return None
        except Exception:
            logger.warning(
                "Could not inspect retained MCP activity %s",
                activity_id,
                exc_info=True,
            )
            return None
        if described is None:
            return None
        activity_input, call = described
        return (
            call
            if activity_input.account_id == account_id
            and activity_input.server_name == server_name
            else None
        )

    calls = await asyncio.gather(
        *(describe(execution.activity_id) for execution in executions)
    )
    return sorted(
        (call for call in calls if call is not None),
        key=lambda call: call.scheduled_at,
        reverse=True,
    )
