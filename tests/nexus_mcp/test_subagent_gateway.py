from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import nexusrpc
import pytest
import temporalio.nexus
from durable_tools_gateway.generated import (
    DispatchSubagentTurnInput,
    StartSubagentInput,
    StopSubagentInput,
)
from durable_tools_gateway.registry import SubagentInstanceRoute, ToolRegistryWorkflow
from durable_tools_gateway.registry_service_handler import (
    RegistryServiceHandler,
    SubagentDispatchOutput,
    SubagentStartResult,
    SubagentStopInput,
    _check_subagent_response,
    subagent_dispatch_activity_id,
    subagent_proxy_activity,
    subagent_start_activity,
    subagent_stop_activity,
)
from durable_tools_gateway.registry_service_handler import (
    SubagentStartInput as ActivityStartInput,
)
from temporalio.common import ActivityIDConflictPolicy
from temporalio.exceptions import ApplicationError

_FAKE_NEXUS_INFO = temporalio.nexus.Info(
    endpoint="test-endpoint", namespace="test-namespace", task_queue="test-task-queue"
)


def _context(request_id: str = "request-1") -> MagicMock:
    return MagicMock(request_id=request_id)


def _client(
    *, provider_instance_id: str = "provider-instance-1", turn_number: int = 1
) -> MagicMock:
    client = MagicMock()
    handle = MagicMock()

    async def query(method, *args, **kwargs):
        if method is ToolRegistryWorkflow.find_subagent:
            return "http://provider"
        if method is ToolRegistryWorkflow.find_subagent_instance:
            return SubagentInstanceRoute(
                alias="writer",
                url="http://provider-v1",
                provider_instance_id=provider_instance_id,
            )
        raise AssertionError(f"unexpected query: {method}")

    async def execute_activity(activity, input, **kwargs):
        if activity is subagent_start_activity:
            return SubagentStartResult(instance_id=provider_instance_id)
        if activity is subagent_proxy_activity:
            return SubagentDispatchOutput(
                output='{"text":"done"}', turn_id="turn-1", turn_number=turn_number
            )
        if activity is subagent_stop_activity:
            assert isinstance(input, SubagentStopInput)
            return None
        raise AssertionError(f"unexpected activity: {activity}")

    handle.query = AsyncMock(side_effect=query)
    handle.execute_update = AsyncMock()
    client.start_workflow = AsyncMock(return_value=handle)
    client.execute_activity = AsyncMock(side_effect=execute_activity)
    return client


def test_instance_route_survives_factory_reregistration() -> None:
    registry = ToolRegistryWorkflow("account-1")
    route = SubagentInstanceRoute(
        alias="writer",
        url="http://provider-v1",
        provider_instance_id="provider-instance-1",
    )
    with patch("durable_tools_gateway.registry.workflow.logger"):
        registry.register_subagent("writer", "http://provider-v1")
        registry.bind_subagent_instance("gateway-instance-1", route)
        registry.register_subagent("writer", "http://provider-v2")

    assert (
        registry.find_subagent_instance("gateway-instance-1") == route
    )
    registry.unbind_subagent_instance("gateway-instance-1")
    assert registry.find_subagent_instance("gateway-instance-1") is None


@patch("temporalio.nexus.info", return_value=_FAKE_NEXUS_INFO)
async def test_start_binds_a_gateway_instance(_mock_info: MagicMock) -> None:
    client = _client(provider_instance_id="provider-instance-7")
    output = await RegistryServiceHandler(client).start_subagent(
        _context("start-request"),
        StartSubagentInput(account_id="agent-1", alias="writer"),
    )

    activity_input = client.execute_activity.await_args.args[1]
    bind_args = client.start_workflow.return_value.execute_update.await_args.kwargs[
        "args"
    ]
    assert output.instance_id == bind_args[0]
    assert bind_args[1] == SubagentInstanceRoute(
        alias="writer",
        url="http://provider",
        provider_instance_id="provider-instance-7",
    )
    assert activity_input.idempotency_key == "agent-1:writer:start:start-request"
    assert client.execute_activity.await_args.kwargs[
        "schedule_to_close_timeout"
    ].total_seconds() == 50


@patch("temporalio.nexus.info", return_value=_FAKE_NEXUS_INFO)
async def test_dispatch_key_includes_instance_id(_mock_info: MagicMock) -> None:
    client = _client()
    output = await RegistryServiceHandler(client).dispatch_subagent_turn(
        _context("turn-request"),
        DispatchSubagentTurnInput(
            account_id="agent-1",
            instance_id="gateway-instance-1",
            msg_type="ask",
            payload='{"text":"hello"}',
            expected_turn=1,
        ),
    )

    activity_input = client.execute_activity.await_args.args[1]
    assert activity_input.idempotency_key == "agent-1:gateway-instance-1:1"
    assert activity_input.instance_id == "provider-instance-1"
    assert activity_input.url == "http://provider-v1"
    assert output.turn_number == 1
    assert subagent_dispatch_activity_id("gateway-instance-1", 1) == (
        "subagent-dispatch-gateway-instance-1-1"
    )
    assert client.execute_activity.await_args.kwargs["id"] == (
        "subagent-dispatch-gateway-instance-1-1"
    )
    assert client.execute_activity.await_args.kwargs["id_conflict_policy"] == (
        ActivityIDConflictPolicy.USE_EXISTING
    )
    assert client.execute_activity.await_args.kwargs[
        "schedule_to_close_timeout"
    ].total_seconds() == 300


@patch("temporalio.nexus.info", return_value=_FAKE_NEXUS_INFO)
async def test_dispatch_rejects_wrong_remote_turn(_mock_info: MagicMock) -> None:
    client = _client(turn_number=2)
    with pytest.raises(nexusrpc.HandlerError):
        await RegistryServiceHandler(client).dispatch_subagent_turn(
            _context(),
            DispatchSubagentTurnInput(
                account_id="agent-1",
                instance_id="gateway-instance-1",
                msg_type="ask",
                payload="{}",
                expected_turn=1,
            ),
        )


@patch("temporalio.nexus.info", return_value=_FAKE_NEXUS_INFO)
async def test_stop_targets_one_instance(_mock_info: MagicMock) -> None:
    client = _client()
    await RegistryServiceHandler(client).stop_subagent(
        _context("stop-request"),
        StopSubagentInput(
            account_id="agent-1", instance_id="gateway-instance-1"
        ),
    )

    activity_input = client.execute_activity.await_args.args[1]
    assert activity_input.instance_id == "provider-instance-1"
    update = client.start_workflow.return_value.execute_update
    assert update.await_args.args[0] is ToolRegistryWorkflow.unbind_subagent_instance
    assert update.await_args.args[1] == "gateway-instance-1"
    assert client.execute_activity.await_args.kwargs[
        "schedule_to_close_timeout"
    ].total_seconds() == 50


async def test_provider_rejection_is_not_retried() -> None:
    response = httpx.Response(
        409,
        json={"detail": "stale turn"},
        request=httpx.Request("POST", "http://provider/sessions"),
    )
    http = AsyncMock()
    http.post.return_value = response
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=http)
    context.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "durable_tools_gateway.registry_service_handler.httpx.AsyncClient",
        return_value=context,
    ):
        with pytest.raises(ApplicationError) as exc_info:
            await subagent_start_activity(
                ActivityStartInput(url="http://provider", idempotency_key="start-1")
            )

    assert exc_info.value.non_retryable
    assert exc_info.value.type == "SubagentProtocolError"


def test_provider_rate_limit_remains_retryable() -> None:
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "http://provider/sessions"),
    )

    with pytest.raises(httpx.HTTPStatusError):
        _check_subagent_response(response)
