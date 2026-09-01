"""Handle Nexus calls for the Durable Tools Gateway.

The gateway routes MCP servers and HTTP subagent factories through standalone
activities.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import timedelta
from typing import Any, TypeVar

import httpx
import nexusrpc
import nexusrpc.handler
import temporalio.nexus
from authoring import validate_service_name
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from nexusrpc.handler import StartOperationContext
from pydantic import BaseModel, Field, ValidationError
from temporalio import activity
from temporalio.client import ActivityFailureError, Client
from temporalio.common import (
    ActivityIDConflictPolicy,
    RetryPolicy,
    WorkflowIDConflictPolicy,
)
from temporalio.exceptions import ApplicationError

from .generated import (
    CallToolInput,
    CallToolOutput,
    CallToolOutputResult,
    DeregisterInput,
    DeregisterSubagentInput,
    DispatchSubagentTurnInput,
    DispatchSubagentTurnOutput,
    ListAccountEntriesInput,
    ListAccountEntriesOutput,
    ListAccountEntriesOutputRemoteTools,
    ListAccountEntriesOutputRemoteToolsValueItem,
    RegisterExternalInput,
    RegisterSubagentInput,
    RegistryService,
    StartSubagentInput,
    StartSubagentOutput,
    StopSubagentInput,
)
from .registry import (
    REGISTRY_TASK_QUEUE,
    SubagentInstanceRoute,
    ToolRegistryWorkflow,
    account_registry_workflow_id,
    fetch_external_tools,
)

REGISTRY_NEXUS_ENDPOINT = "mcp-registry-endpoint"

logger = logging.getLogger(__name__)
_ResponseModel = TypeVar("_ResponseModel", bound=BaseModel)


def subagent_dispatch_activity_id(instance_id: str, turn_number: int) -> str:
    """Address one third-party subagent turn without a Visibility lookup."""
    return f"subagent-dispatch-{instance_id}-{turn_number}"


class ExternalMCPCallInput(BaseModel):
    """Input for an HTTP call to a 3rd-party MCP server."""

    server_url: str
    tool_name: str
    arguments: dict[str, Any]


async def _heartbeat_every(seconds: float) -> None:
    """Heartbeat on a fixed interval until cancelled."""
    while True:
        await asyncio.sleep(seconds)
        activity.heartbeat()


@activity.defn
async def mcp_proxy_activity(input: ExternalMCPCallInput) -> CallToolResult:
    """Call one tool on an external MCP server over Streamable HTTP.

    Returns CallToolResult as-is: a tool-level error (isError=True) is data, not
    raised. Only a real RPC/transport failure raises.
    """
    activity.logger.info("[proxy-activity] calling %r on %s", input.tool_name, input.server_url)
    heartbeat_task = asyncio.create_task(_heartbeat_every(15))
    try:
        async with streamable_http_client(input.server_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(input.tool_name, input.arguments)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
    activity.logger.info("[proxy-activity] %r completed  is_error=%s", input.tool_name, result.isError)
    return result


class SubagentStartInput(BaseModel):
    url: str
    idempotency_key: str


class SubagentStartResult(BaseModel):
    instance_id: str = Field(min_length=1)


class SubagentDispatchInput(BaseModel):
    """Input for one HTTP call to a non-Nexus subagent's turn endpoint."""

    url: str
    instance_id: str
    msg_type: str
    payload: str  # JSON-encoded handler input
    expected_turn: int
    idempotency_key: str


class SubagentDispatchOutput(BaseModel):
    output: str
    turn_id: str
    turn_number: int


class SubagentTurnResponse(BaseModel):
    output: dict[str, Any]
    turn_id: str = Field(min_length=1)
    turn_number: int = Field(ge=1)


class SubagentStopInput(BaseModel):
    url: str
    instance_id: str


def _provider_url(url: str) -> str:
    return url.rstrip("/")


def _check_subagent_response(response: httpx.Response) -> None:
    """Reject provider protocol errors without retrying them."""
    if 400 <= response.status_code < 500 and response.status_code not in {
        408,
        425,
        429,
    }:
        try:
            body = response.json()
            detail = (
                body.get("detail", response.text)
                if isinstance(body, dict)
                else response.text
            )
        except ValueError:
            detail = response.text
        raise ApplicationError(
            f"subagent provider returned HTTP {response.status_code}: {detail}",
            type="SubagentProtocolError",
            non_retryable=True,
        )
    response.raise_for_status()


def _parse_subagent_response(
    model: type[_ResponseModel], response: httpx.Response
) -> _ResponseModel:
    """Validate a provider response without retrying invalid data."""
    try:
        return model.model_validate(response.json())
    except (ValueError, ValidationError) as exc:
        raise ApplicationError(
            "subagent provider returned an invalid response body",
            type="SubagentProtocolError",
            non_retryable=True,
        ) from exc


@activity.defn
async def subagent_start_activity(input: SubagentStartInput) -> SubagentStartResult:
    """Start one instance from an HTTP subagent provider."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.post(
            f"{_provider_url(input.url)}/sessions",
            json={"idempotency_key": input.idempotency_key},
        )
        _check_subagent_response(resp)
    return _parse_subagent_response(SubagentStartResult, resp)


@activity.defn
async def subagent_proxy_activity(input: SubagentDispatchInput) -> SubagentDispatchOutput:
    """POST one turn to a non-Nexus subagent's HTTP endpoint.

    The remote subagent must deduplicate requests by ``idempotency_key``.
    """
    activity.logger.info(
        "[subagent-proxy] dispatching turn %d to %s", input.expected_turn, input.url
    )
    heartbeat_task = asyncio.create_task(_heartbeat_every(15))
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            resp = await http.post(
                f"{_provider_url(input.url)}/sessions/{input.instance_id}/turns",
                json={
                    "idempotency_key": input.idempotency_key,
                    "msg_type": input.msg_type,
                    "payload": input.payload,
                    "expected_turn": input.expected_turn,
                },
            )
            _check_subagent_response(resp)
            data = _parse_subagent_response(SubagentTurnResponse, resp)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
    activity.logger.info("[subagent-proxy] turn %d completed", input.expected_turn)
    return SubagentDispatchOutput(
        output=json.dumps(data.output),
        turn_id=data.turn_id,
        turn_number=data.turn_number,
    )


@activity.defn
async def subagent_stop_activity(input: SubagentStopInput) -> None:
    """Request that an HTTP subagent close its session."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.post(
            f"{_provider_url(input.url)}/sessions/{input.instance_id}/close"
        )
        _check_subagent_response(resp)


async def _fetch_tools_grouped(client: Client, servers: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Fetch each {alias: url}'s tool list live, grouped by alias. Unreachable servers
    are logged and skipped."""
    if not servers:
        return {}

    async def _fetch_one(name: str, url: str) -> list[dict[str, Any]]:
        return await client.execute_activity(
            fetch_external_tools,
            args=[name, url],
            id=f"mcp-list-tools-{name}-{uuid.uuid4()}",
            task_queue=temporalio.nexus.info().task_queue,
            schedule_to_close_timeout=timedelta(seconds=60),
            start_to_close_timeout=timedelta(seconds=45),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

    results = await asyncio.gather(
        *(_fetch_one(name, url) for name, url in servers.items()), return_exceptions=True
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for (name, url), result in zip(servers.items(), results):
        if isinstance(result, BaseException):
            logger.warning("[registry] could not fetch tools from %r (%s): %s", name, url, result)
            continue
        grouped[name] = result
    return grouped


@nexusrpc.handler.service_handler(service=RegistryService)
class RegistryServiceHandler:
    """Signals/queries ToolRegistryWorkflow and dispatches tool calls, for callers in
    any namespace, via the mcp-registry-endpoint Nexus endpoint."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def _account_handle(self, account_id: str):
        if not account_id.strip():
            raise nexusrpc.HandlerError(
                "account_id is required",
                type=nexusrpc.HandlerErrorType.BAD_REQUEST,
            )
        return await self._client.start_workflow(
            ToolRegistryWorkflow.run,
            account_id,
            id=account_registry_workflow_id(account_id),
            task_queue=REGISTRY_TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )

    @nexusrpc.handler.sync_operation
    async def register_external(
        self, ctx: StartOperationContext, input: RegisterExternalInput
    ) -> None:
        """Register a 3rd-party MCP server under one account_id."""
        account_id = input.account_id or ""
        name = input.name or ""
        if not account_id:
            raise nexusrpc.HandlerError("account_id is required", type=nexusrpc.HandlerErrorType.BAD_REQUEST)
        try:
            validate_service_name(name)
        except ValueError as exc:
            raise nexusrpc.HandlerError(str(exc), type=nexusrpc.HandlerErrorType.BAD_REQUEST) from exc
        if not input.url:
            raise nexusrpc.HandlerError("url is required", type=nexusrpc.HandlerErrorType.BAD_REQUEST)

        handle = await self._account_handle(account_id)
        await handle.signal(ToolRegistryWorkflow.register_external, args=[name, input.url])

    @nexusrpc.handler.sync_operation
    async def deregister(
        self, ctx: StartOperationContext, input: DeregisterInput
    ) -> None:
        """Remove one registration under one account_id."""
        handle = await self._account_handle(input.account_id)
        await handle.signal(ToolRegistryWorkflow.deregister, input.name)

    @nexusrpc.handler.sync_operation
    async def list_account_entries(
        self, ctx: StartOperationContext, input: ListAccountEntriesInput
    ) -> ListAccountEntriesOutput:
        """Current 3rd-party tool lists (grouped by alias), for one account_id. Tools are
        fetched live, every call."""
        account_id = input.account_id or ""
        if not account_id:
            raise nexusrpc.HandlerError("account_id is required", type=nexusrpc.HandlerErrorType.BAD_REQUEST)
        handle = await self._account_handle(account_id)
        entries = await handle.query(ToolRegistryWorkflow.list_account_entries)
        remote_tools = await _fetch_tools_grouped(self._client, entries.remote_servers)
        # nex-gen wraps map-shaped (additionalProperties) fields in a named type instead
        # of a plain dict -- wrap _fetch_tools_grouped's plain dict/list-of-dicts here.
        return ListAccountEntriesOutput(
            remote_tools=ListAccountEntriesOutputRemoteTools(
                additional_properties={
                    alias: [
                        ListAccountEntriesOutputRemoteToolsValueItem(additional_properties=tool)
                        for tool in tools
                    ]
                    for alias, tools in remote_tools.items()
                }
            )
        )

    @nexusrpc.handler.sync_operation
    async def call_tool(
        self, ctx: StartOperationContext, input: CallToolInput
    ) -> CallToolOutput:
        """Invoke one tool via a standalone activity (Nexus + SAA, no backing workflow).
        Raises nexusrpc.HandlerError explicitly -- a bare exception defaults to UNKNOWN,
        which retries forever instead of surfacing to the caller."""
        account_id = input.account_id or ""
        alias = input.alias or ""
        name = input.name or ""
        if not account_id or not alias:
            raise nexusrpc.HandlerError(
                "account_id and alias are required", type=nexusrpc.HandlerErrorType.BAD_REQUEST
            )
        operation = name.removeprefix(f"{alias}_")
        if operation == name:
            raise nexusrpc.HandlerError(
                f"Tool name {name!r} does not start with alias {alias!r}",
                type=nexusrpc.HandlerErrorType.BAD_REQUEST,
            )

        handle = await self._account_handle(account_id)
        url: str | None = await handle.query(ToolRegistryWorkflow.find, alias)
        if url is None:
            raise nexusrpc.HandlerError(
                f"{alias!r} is not registered for account {account_id!r}.",
                type=nexusrpc.HandlerErrorType.NOT_FOUND,
            )

        # nex-gen wraps map-shaped (additionalProperties) fields in a named type instead
        # of a plain dict.
        arguments = input.arguments.additional_properties if input.arguments is not None else {}
        try:
            result = await self._client.execute_activity(
                mcp_proxy_activity,
                ExternalMCPCallInput(server_url=url, tool_name=operation, arguments=arguments),
                id=f"mcp-proxy-{uuid.uuid4()}",
                task_queue=temporalio.nexus.info().task_queue,
                start_to_close_timeout=timedelta(minutes=5),
                heartbeat_timeout=timedelta(seconds=30),
                # No retries: the tool may not be idempotent (e.g. sends an email).
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityFailureError as exc:
            # Real RPC/transport failure. A tool-level error is data on `result` instead.
            raise nexusrpc.HandlerError(
                str(exc.cause or exc), type=nexusrpc.HandlerErrorType.INTERNAL, retryable_override=False
            ) from exc
        return CallToolOutput(
            result=CallToolOutputResult(additional_properties=result.model_dump(mode="json"))
        )

    @nexusrpc.handler.sync_operation
    async def register_subagent(
        self, ctx: StartOperationContext, input: RegisterSubagentInput
    ) -> None:
        """Register a non-Nexus subagent's URL under one account_id."""
        account_id = input.account_id or ""
        alias = input.alias or ""
        if not account_id or not alias:
            raise nexusrpc.HandlerError(
                "account_id and alias are required", type=nexusrpc.HandlerErrorType.BAD_REQUEST
            )
        if not input.url:
            raise nexusrpc.HandlerError("url is required", type=nexusrpc.HandlerErrorType.BAD_REQUEST)

        handle = await self._account_handle(account_id)
        await handle.signal(ToolRegistryWorkflow.register_subagent, args=[alias, input.url])

    @nexusrpc.handler.sync_operation
    async def deregister_subagent(
        self, ctx: StartOperationContext, input: DeregisterSubagentInput
    ) -> None:
        """Remove one subagent registration under one account_id."""
        handle = await self._account_handle(input.account_id)
        await handle.signal(
            ToolRegistryWorkflow.deregister_subagent, input.alias
        )

    @nexusrpc.handler.sync_operation
    async def start_subagent(
        self, ctx: StartOperationContext, input: StartSubagentInput
    ) -> StartSubagentOutput:
        """Start one instance from a registered provider."""
        account_id = input.account_id
        alias = input.alias
        if not account_id or not alias:
            raise nexusrpc.HandlerError(
                "account_id and alias are required", type=nexusrpc.HandlerErrorType.BAD_REQUEST
            )

        handle = await self._account_handle(account_id)
        url: str | None = await handle.query(
            ToolRegistryWorkflow.find_subagent, alias
        )
        if url is None:
            raise nexusrpc.HandlerError(
                f"subagent {alias!r} is not registered for account {account_id!r}.",
                type=nexusrpc.HandlerErrorType.NOT_FOUND,
            )

        idempotency_key = f"{account_id}:{alias}:start:{ctx.request_id}"
        instance_id = uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key).hex
        try:
            result = await self._client.execute_activity(
                subagent_start_activity,
                SubagentStartInput(url=url, idempotency_key=idempotency_key),
                id=f"subagent-start-{ctx.request_id}",
                task_queue=temporalio.nexus.info().task_queue,
                schedule_to_close_timeout=timedelta(seconds=50),
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityFailureError as exc:
            raise nexusrpc.HandlerError(
                str(exc.cause or exc),
                type=nexusrpc.HandlerErrorType.INTERNAL,
                retryable_override=False,
            ) from exc
        await handle.execute_update(
            ToolRegistryWorkflow.bind_subagent_instance,
            args=[
                instance_id,
                SubagentInstanceRoute(
                    alias=alias,
                    url=url,
                    provider_instance_id=result.instance_id,
                ),
            ],
            id=f"subagent-bind-{instance_id}",
        )
        return StartSubagentOutput(instance_id=instance_id)

    @nexusrpc.handler.sync_operation
    async def dispatch_subagent_turn(
        self, ctx: StartOperationContext, input: DispatchSubagentTurnInput
    ) -> DispatchSubagentTurnOutput:
        """Send one turn through a retryable standalone activity."""
        account_id = input.account_id
        instance_id = input.instance_id
        if not account_id or not instance_id or input.expected_turn < 1:
            raise nexusrpc.HandlerError(
                "account_id, instance_id, and a positive expected_turn are required",
                type=nexusrpc.HandlerErrorType.BAD_REQUEST,
            )

        handle = await self._account_handle(account_id)
        route: SubagentInstanceRoute | None = await handle.query(
            ToolRegistryWorkflow.find_subagent_instance,
            instance_id,
        )
        if route is None:
            raise nexusrpc.HandlerError(
                f"subagent instance {instance_id!r} was not found for account {account_id!r}.",
                type=nexusrpc.HandlerErrorType.NOT_FOUND,
            )

        idempotency_key = f"{account_id}:{instance_id}:{input.expected_turn}"
        try:
            result = await self._client.execute_activity(
                subagent_proxy_activity,
                SubagentDispatchInput(
                    url=route.url,
                    instance_id=route.provider_instance_id,
                    msg_type=input.msg_type,
                    payload=input.payload,
                    expected_turn=input.expected_turn,
                    idempotency_key=idempotency_key,
                ),
                id=subagent_dispatch_activity_id(instance_id, input.expected_turn),
                id_conflict_policy=ActivityIDConflictPolicy.USE_EXISTING,
                task_queue=temporalio.nexus.info().task_queue,
                schedule_to_close_timeout=timedelta(minutes=5),
                start_to_close_timeout=timedelta(seconds=75),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityFailureError as exc:
            raise nexusrpc.HandlerError(
                str(exc.cause or exc), type=nexusrpc.HandlerErrorType.INTERNAL, retryable_override=False
            ) from exc
        if result.turn_number != input.expected_turn:
            raise nexusrpc.HandlerError(
                f"subagent {instance_id!r} returned turn {result.turn_number}; "
                f"expected {input.expected_turn}",
                type=nexusrpc.HandlerErrorType.INTERNAL,
                retryable_override=False,
            )
        return DispatchSubagentTurnOutput(
            output=result.output, turn_id=result.turn_id, turn_number=result.turn_number
        )

    @nexusrpc.handler.sync_operation
    async def stop_subagent(
        self, ctx: StartOperationContext, input: StopSubagentInput
    ) -> None:
        """Close a subagent instance."""
        account_id = input.account_id
        instance_id = input.instance_id
        if not account_id or not instance_id:
            raise nexusrpc.HandlerError(
                "account_id and instance_id are required",
                type=nexusrpc.HandlerErrorType.BAD_REQUEST,
            )
        handle = await self._account_handle(account_id)
        route: SubagentInstanceRoute | None = await handle.query(
            ToolRegistryWorkflow.find_subagent_instance,
            instance_id,
        )
        if route is None:
            return  # Stop is idempotent.
        try:
            await self._client.execute_activity(
                subagent_stop_activity,
                SubagentStopInput(
                    url=route.url,
                    instance_id=route.provider_instance_id,
                ),
                id=f"subagent-stop-{ctx.request_id}",
                task_queue=temporalio.nexus.info().task_queue,
                schedule_to_close_timeout=timedelta(seconds=50),
                start_to_close_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except ActivityFailureError as exc:
            raise nexusrpc.HandlerError(
                str(exc.cause or exc),
                type=nexusrpc.HandlerErrorType.INTERNAL,
                retryable_override=False,
            ) from exc
        await handle.execute_update(
            ToolRegistryWorkflow.unbind_subagent_instance,
            instance_id,
            id=f"subagent-unbind-{ctx.request_id}",
        )
