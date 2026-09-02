from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import nexusrpc
import pytest
import temporalio.nexus
from a2a.types import Message, Part, Role, SendMessageRequest
from durable_tools_gateway.generated import (
    DispatchSubagentTurnInput,
    StartSubagentInput,
    StopSubagentInput,
)
from durable_tools_gateway.registry import SubagentInstanceRoute, ToolRegistryWorkflow
from durable_tools_gateway.registry_service_handler import (
    GatewayA2AServiceHandler,
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

    assert registry.find_subagent_instance("gateway-instance-1") == route
    registry.unbind_subagent_instance("gateway-instance-1")
    assert registry.find_subagent_instance("gateway-instance-1") is None


def test_allocated_instance_route_can_bind_its_provider_task() -> None:
    registry = ToolRegistryWorkflow("account-1")
    allocated = SubagentInstanceRoute(
        alias="writer",
        url="http://provider-v1",
        provider_instance_id="",
    )
    bound = SubagentInstanceRoute(
        alias="writer",
        url="http://provider-v1",
        provider_instance_id="provider-instance-1",
    )

    registry.bind_subagent_instance("gateway-instance-1", allocated)
    registry.bind_subagent_instance("gateway-instance-1", bound)

    assert registry.find_subagent_instance("gateway-instance-1") == bound


def test_bound_instance_route_cannot_change_provider_task() -> None:
    registry = ToolRegistryWorkflow("account-1")
    registry.bind_subagent_instance(
        "gateway-instance-1",
        SubagentInstanceRoute(
            alias="writer",
            url="http://provider-v1",
            provider_instance_id="provider-instance-1",
        ),
    )

    with pytest.raises(ApplicationError, match="has a different route"):
        registry.bind_subagent_instance(
            "gateway-instance-1",
            SubagentInstanceRoute(
                alias="writer",
                url="http://provider-v1",
                provider_instance_id="provider-instance-2",
            ),
        )


@patch("temporalio.nexus.info", return_value=_FAKE_NEXUS_INFO)
async def test_gateway_a2a_send_binds_the_provider_task(
    _mock_info: MagicMock,
) -> None:
    registry = ToolRegistryWorkflow("account-1")
    registry.bind_subagent_instance(
        "gateway-instance-1",
        SubagentInstanceRoute(
            alias="writer",
            url="http://provider-v1",
            provider_instance_id="",
        ),
    )
    handle = MagicMock()

    async def query(method, task_id, **_kwargs):
        assert method is ToolRegistryWorkflow.find_subagent_instance
        return registry.find_subagent_instance(task_id)

    async def update(method, *, args, **_kwargs):
        assert method is ToolRegistryWorkflow.bind_subagent_instance
        registry.bind_subagent_instance(*args)

    handle.query = AsyncMock(side_effect=query)
    handle.execute_update = AsyncMock(side_effect=update)
    client = MagicMock()
    client.start_workflow = AsyncMock(return_value=handle)
    client.execute_activity = AsyncMock(
        return_value=SubagentDispatchOutput(
            output='{"text":"done"}',
            turn_id="provider-instance-1-turn-1",
            turn_number=1,
            provider_instance_id="provider-instance-1",
        )
    )
    request = SendMessageRequest(
        message=Message(
            message_id="message-1",
            role=Role.ROLE_USER,
            task_id="gateway-instance-1",
            context_id="gateway-instance-1",
            parts=[Part(text="hello")],
            metadata={"temporal.io/message-type": "ask"},
        ),
        metadata={
            "account_id": "account-1",
            "agent_id": "writer",
            "expected_turn": 1,
        },
    )

    response = await GatewayA2AServiceHandler(client).send_message(
        _context(), request
    )

    assert response.task.artifacts[0].parts[0].text == "done"
    assert registry.find_subagent_instance(
        "gateway-instance-1"
    ) == SubagentInstanceRoute(
        alias="writer",
        url="http://provider-v1",
        provider_instance_id="provider-instance-1",
    )
    assert handle.execute_update.await_args.kwargs["id"] == (
        "a2a-provider-bind-gateway-instance-1-provider-instance-1"
    )


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
    assert (
        client.execute_activity.await_args.kwargs[
            "schedule_to_close_timeout"
        ].total_seconds()
        == 50
    )


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
    assert (
        client.execute_activity.await_args.kwargs[
            "schedule_to_close_timeout"
        ].total_seconds()
        == 300
    )


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
        StopSubagentInput(account_id="agent-1", instance_id="gateway-instance-1"),
    )

    activity_input = client.execute_activity.await_args.args[1]
    assert activity_input.instance_id == "provider-instance-1"
    update = client.start_workflow.return_value.execute_update
    assert update.await_args.args[0] is ToolRegistryWorkflow.unbind_subagent_instance
    assert update.await_args.args[1] == "gateway-instance-1"
    assert (
        client.execute_activity.await_args.kwargs[
            "schedule_to_close_timeout"
        ].total_seconds()
        == 50
    )


async def test_a2a_task_allocation_is_deterministic_and_does_not_call_provider() -> (
    None
):
    first = await subagent_start_activity(
        ActivityStartInput(url="http://provider", idempotency_key="start-1")
    )
    second = await subagent_start_activity(
        ActivityStartInput(url="http://provider", idempotency_key="start-1")
    )

    assert first == second
    assert first.instance_id


def test_provider_rate_limit_remains_retryable() -> None:
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "http://provider/sessions"),
    )

    with pytest.raises(httpx.HTTPStatusError):
        _check_subagent_response(response)
