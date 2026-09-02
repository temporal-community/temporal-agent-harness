from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from durable_tools_gateway.registry import (
    AccountEntries,
    AgentRegistration,
    GlobalCatalogWorkflow,
    NexusMCPServerRegistration,
    SessionEvent,
    SessionRecord,
    ToolRegistryWorkflow,
)
from durable_tools_gateway.registry_service_handler import (
    SubagentDispatchInput,
    SubagentDispatchOutput,
)
from durable_tools_gateway.resources import (
    TEXT_AGENT_HANDLER,
    AccountResourceRegistration,
    ResourceDescriptor,
)
from durable_tools_gateway.tool_history import ToolCallRecord
from durable_tools_gateway.web import (
    _replay_external_activity_turn,
    create_account_agent_app,
)
from fastapi.testclient import TestClient
from temporalio.api.workflowservice.v1 import DescribeActivityExecutionResponse
from temporalio.contrib.pydantic import pydantic_data_converter

from temporal_agent_harness.nexus_agent_adapter.generated import (
    AgentInterfaceOutput,
    SendMessageOutput,
)


def _app_client(
    *,
    session_override: SessionRecord | None = None,
    agent_override: AgentRegistration | None = None,
    session_events: list[SessionEvent] | None = None,
    entries_override: AccountEntries | None = None,
    catalog_override: list[ResourceDescriptor] | None = None,
) -> tuple[TestClient, MagicMock, MagicMock]:
    agent = agent_override or AgentRegistration(
        agent_id="assistant",
        kind="harness_nexus",
        label="Assistant",
        description="A registered harness agent",
        nexus_endpoint="assistant-endpoint",
    )
    session = session_override or SessionRecord(
        account_id="account-1",
        session_id="session-1",
        agent_id="assistant",
        provider_session_id="session-1",
        created_at=123.0,
        label="Session 1",
        is_message_queuing_enabled=True,
    )
    handle = MagicMock()

    async def query(method, *args, **kwargs):
        if method is ToolRegistryWorkflow.list_agents:
            return [agent]
        if method is ToolRegistryWorkflow.get_agent:
            return agent
        if method is ToolRegistryWorkflow.resolve_session:
            return session
        if method is ToolRegistryWorkflow.list_sessions:
            return [session]
        if method is ToolRegistryWorkflow.list_account_entries:
            return entries_override or AccountEntries(
                remote_servers={"weather": "http://weather"},
                subagent_providers={"writer": "http://writer"},
            )
        if method is ToolRegistryWorkflow.poll_session_events:
            return session_events or []
        if method is GlobalCatalogWorkflow.list_resources:
            return catalog_override or []
        if method is GlobalCatalogWorkflow.get_resource:
            resource_id = args[0]
            return next(
                (
                    item
                    for item in catalog_override or []
                    if item.resource_id == resource_id
                ),
                None,
            )
        raise AssertionError(f"unexpected query {method}")

    async def update(method, *args, **kwargs):
        if method is ToolRegistryWorkflow.create_session:
            return session
        if method is ToolRegistryWorkflow.register_agent:
            return args[0]
        if method is ToolRegistryWorkflow.register_nexus_mcp_server:
            return args[0]
        if method is ToolRegistryWorkflow.mark_session_started:
            update_args = kwargs["args"]
            return replace(session, has_started=True, current_turn=update_args[1])
        if method is ToolRegistryWorkflow.reconcile_spawned_agents:
            return []
        if method is ToolRegistryWorkflow.close_session:
            return replace(session, closed=True)
        if method is ToolRegistryWorkflow.install_resource:
            return AccountResourceRegistration(args[0], installed_at=123.0)
        if method is ToolRegistryWorkflow.remove_resource:
            return None
        raise AssertionError(f"unexpected update {method}")

    handle.query = AsyncMock(side_effect=query)
    handle.execute_update = AsyncMock(side_effect=update)
    temporal = MagicMock()
    temporal.start_workflow = AsyncMock(return_value=handle)
    temporal.execute_workflow = AsyncMock()
    nexus = MagicMock()
    nexus.execute_operation = AsyncMock(return_value=AgentInterfaceOutput(handlers=[]))
    temporal.create_nexus_client.return_value = nexus
    temporal.execute_activity = AsyncMock()
    app = create_account_agent_app(
        "account-1", temporal_client=temporal, static_dir=None
    )
    return TestClient(app), handle, temporal


def test_account_agents_and_sessions_come_from_the_registry() -> None:
    client, _, _ = _app_client()
    with client:
        agents = client.get("/api/agents")
        sessions = client.get("/api/sessions")
        account = client.get("/api/account")

    assert agents.status_code == 200
    assert agents.json()["agents"][0]["workflow_type"] == "assistant"
    assert sessions.status_code == 200
    assert sessions.json()[0]["workflow_id"] == "session-1"
    assert sessions.json()[0]["execution_status"] == "NOT_STARTED"
    assert account.status_code == 200
    assert account.json()["account_id"] == "account-1"
    assert account.json()["agents"][0]["active_session_count"] == 1
    assert account.json()["mcp_servers"][0]["name"] == "weather"


def test_catalog_installs_shared_descriptor_into_account_registry() -> None:
    catalog_agent = ResourceDescriptor(
        "catalog-agent",
        2,
        "agent",
        "nexus",
        "Catalog Agent",
        "Published globally",
        "catalog-agent-endpoint",
        "AgentService",
        (TEXT_AGENT_HANDLER,),
    )
    client, handle, _ = _app_client(catalog_override=[catalog_agent])

    with client:
        catalog = client.get("/api/catalog")
        installed = client.post("/api/catalog/catalog-agent/register")
        removed = client.delete("/api/catalog/catalog-agent/register")

    assert catalog.status_code == 200
    assert catalog.json()["resources"][0]["installed"] is False
    assert installed.status_code == 200
    assert installed.json()["revision"] == 2
    assert removed.status_code == 200
    install_call = next(
        call
        for call in handle.execute_update.await_args_list
        if call.args[0] is ToolRegistryWorkflow.install_resource
    )
    assert install_call.args[1] == catalog_agent


def test_native_mcp_history_is_scoped_through_registered_metadata() -> None:
    registration = NexusMCPServerRegistration(
        name="native-demo",
        endpoint="native-demo-endpoint",
        service="demo-nexus",
    )
    client, handle, _ = _app_client(
        entries_override=AccountEntries(
            remote_servers={"weather": "http://weather"},
            nexus_servers={"native-demo": registration},
        )
    )
    call = ToolCallRecord(
        call_id="nexus:default:session-1:12",
        server_name="native-demo",
        transport="nexus",
        tool_name="get_lucky_number",
        status="completed",
        scheduled_at=123.0,
        namespace="default",
        execution_id="12",
        workflow_id="session-1",
        agent_id="assistant",
        input={"topic": "Temporal"},
        output="7",
    )

    with (
        patch(
            "durable_tools_gateway.web.scan_native_tool_calls",
            new=AsyncMock(return_value=[call]),
        ) as scan,
        client,
    ):
        registered = client.post(
            "/api/account/mcp-servers",
            json={
                "name": "native-demo",
                "endpoint": "native-demo-endpoint",
                "service": "demo-nexus",
            },
        )
        account = client.get("/api/account")
        history = client.get("/api/mcp-servers/native-demo/tool-calls")

    assert registered.status_code == 200
    assert registered.json() == {
        "name": "native-demo",
        "endpoint": "native-demo-endpoint",
        "service": "demo-nexus",
    }
    assert account.json()["mcp_servers"] == [
        {
            "name": "native-demo",
            "endpoint": "native-demo-endpoint",
            "kind": "nexus",
            "service": "demo-nexus",
        },
        {
            "name": "weather",
            "endpoint": "http://weather",
            "kind": "external_http",
            "service": None,
        },
    ]
    assert history.status_code == 200
    assert history.json()[0]["tool_name"] == "get_lucky_number"
    scan.assert_awaited_once()
    assert any(
        call.args[0] is ToolRegistryWorkflow.register_nexus_mcp_server
        for call in handle.execute_update.await_args_list
    )


def test_external_mcp_history_uses_the_gateway_activity_reader() -> None:
    client, _, _ = _app_client()
    call = ToolCallRecord(
        call_id="activity:gateway:mcp-proxy-1",
        server_name="weather",
        transport="external_http",
        tool_name="forecast",
        status="completed",
        scheduled_at=123.0,
        namespace="gateway",
        execution_id="mcp-proxy-1",
        input={"city": "New York"},
        output={"temperature": 70},
    )

    with (
        patch(
            "durable_tools_gateway.web.scan_external_tool_calls",
            new=AsyncMock(return_value=[call]),
        ) as scan,
        client,
    ):
        history = client.get("/api/mcp-servers/weather/tool-calls")

    assert history.status_code == 200
    assert history.json()[0]["execution_id"] == "mcp-proxy-1"
    assert scan.await_args.kwargs == {
        "account_id": "account-1",
        "server_name": "weather",
    }


def test_fresh_native_session_is_created_without_a_provider_workflow() -> None:
    client, handle, _ = _app_client()
    with client:
        response = client.post(
            "/api/sessions",
            json={
                "agent_workflow_type": "assistant",
                "is_message_queuing_enabled": True,
            },
        )
        attach = client.get("/api/attach?session_id=session-1&from_offset=0")

    assert response.status_code == 200
    assert response.json()["closed"] is False
    assert attach.status_code == 200
    assert attach.content == b""
    assert (
        handle.execute_update.await_args_list[0].args[0]
        is ToolRegistryWorkflow.create_session
    )


def test_provider_session_alias_resolves_through_the_account_registry() -> None:
    spawned = SessionRecord(
        account_id="account-1",
        session_id="session-child",
        agent_id="assistant",
        provider_session_id="provider-child",
        source_session_id="source-child",
        created_at=123.0,
        label="Spawned child",
        is_spawned=True,
        has_started=True,
    )
    client, handle, _ = _app_client(session_override=spawned)

    with client:
        provider_response = client.get("/api/agent-interface/provider-child")
        source_response = client.get("/api/agent-interface/source-child")

    assert provider_response.status_code == 200
    assert source_response.status_code == 200
    resolved_ids = [
        call.args[1]
        for call in handle.query.await_args_list
        if call.args[0] is ToolRegistryWorkflow.resolve_session
    ]
    assert resolved_ids == ["provider-child", "source-child"]


def test_refresh_reconciles_registered_children_from_native_status() -> None:
    started = SessionRecord(
        account_id="account-1",
        session_id="session-1",
        agent_id="assistant",
        provider_session_id="provider-parent",
        created_at=123.0,
        label="Session 1",
        has_started=True,
    )
    client, handle, temporal = _app_client(session_override=started)
    temporal.execute_workflow.return_value = {
        "spawned": [
            {
                "subagent_id": "research-a1b2c3",
                "agent_key": "research",
                "workflow_id": "research-workflow",
                "next_expected_turn": 1,
            }
        ],
        "stopped_source_session_ids": [],
        "next_offset": 18,
        "active": [
            {
                "subagent_id": "research-a1b2c3",
                "agent_key": "research",
                "workflow_id": "research-workflow",
                "next_expected_turn": 3,
            }
        ],
    }

    with client:
        response = client.post("/api/sessions/refresh")

    assert response.status_code == 200
    discovery = temporal.execute_workflow.await_args.args[1]
    assert discovery.session_id == "provider-parent"
    assert discovery.cursor == 0
    sync = next(
        call
        for call in handle.execute_update.await_args_list
        if call.args[0] is ToolRegistryWorkflow.reconcile_spawned_agents
    )
    observation = sync.kwargs["args"][1][0]
    assert observation.agent_key == "research"
    assert sync.kwargs["args"][3] == 18
    assert sync.kwargs["args"][4][0].next_expected_turn == 3


def test_native_send_defers_stale_turn_reconciliation_to_agent_service() -> None:
    client, handle, temporal = _app_client()
    temporal.create_nexus_client.return_value.execute_operation.return_value = (
        SendMessageOutput(
            turn_number=2,
            turn_id="turn-2",
            stream_head_offset=8,
            pending=False,
        )
    )

    with client:
        response = client.post(
            "/api/messages",
            json={
                "session_id": "session-1",
                "message": "hello",
                "expected_turn": 1,
            },
        )

    assert response.status_code == 200
    nexus = temporal.create_nexus_client.return_value
    operation_input = nexus.execute_operation.await_args.args[1]
    assert operation_input.expected_turn is None
    assert nexus.execute_operation.await_args.kwargs["id"].startswith("ui-agent-send-")
    temporal.execute_workflow.assert_not_awaited()
    assert any(
        call.args[0] is ToolRegistryWorkflow.mark_session_started
        for call in handle.execute_update.await_args_list
    )


def test_close_requires_a_decision_when_active_subagents_are_running() -> None:
    started = SessionRecord(
        account_id="account-1",
        session_id="session-1",
        agent_id="assistant",
        provider_session_id="provider-parent",
        created_at=123.0,
        label="Session 1",
        has_started=True,
    )
    client, handle, _ = _app_client(session_override=started)
    execute = AsyncMock(
        return_value={
            "subagent_close_policy": "ask-user",
            "subagents": [
                {
                    "subagent_id": "research-a1b2c3",
                    "agent_key": "research",
                    "workflow_id": "research-workflow",
                    "next_expected_turn": 2,
                }
            ],
        }
    )

    with patch("durable_tools_gateway.web.execute_agent_action", execute), client:
        response = client.post("/api/sessions/session-1/close", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "subagent_close_decision_required"
    assert response.json()["detail"]["subagents"][0]["agent_key"] == "research"
    execute.assert_awaited_once()
    assert execute.await_args.args[1].action == "status"
    assert not any(
        call.args[0] is ToolRegistryWorkflow.close_session
        for call in handle.execute_update.await_args_list
    )


def test_close_applies_the_users_subagent_decision_before_closing() -> None:
    started = SessionRecord(
        account_id="account-1",
        session_id="session-1",
        agent_id="assistant",
        provider_session_id="provider-parent",
        created_at=123.0,
        label="Session 1",
        has_started=True,
    )
    client, handle, _ = _app_client(session_override=started)
    execute = AsyncMock(side_effect=[{"reply": "updated"}, {"closed": True}])

    with patch("durable_tools_gateway.web.execute_agent_action", execute), client:
        response = client.post(
            "/api/sessions/session-1/close",
            json={"subagent_close_policy": "close"},
        )

    assert response.status_code == 200
    actions = [call.args[1] for call in execute.await_args_list]
    assert [action.action for action in actions] == ["operator_command", "close"]
    assert actions[0].values == {
        "name": "subagent-close-policy",
        "arg": "close",
    }
    assert any(
        call.args[0] is ToolRegistryWorkflow.close_session
        for call in handle.execute_update.await_args_list
    )


async def test_external_turn_replays_from_deterministic_activity_history() -> None:
    response = DescribeActivityExecutionResponse()
    response.info.activity_id = "subagent-dispatch-gateway-instance-1-1"
    response.info.activity_type.name = "subagent_proxy_activity"
    response.info.task_queue = "mcp-registry"
    response.info.schedule_time.FromDatetime(
        datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    )
    response.info.close_time.FromDatetime(
        datetime(2026, 9, 1, 12, 0, 2, tzinfo=timezone.utc)
    )
    activity_input = SubagentDispatchInput(
        url="http://writer",
        instance_id="provider-instance-1",
        msg_type="ask",
        payload='{"text":"draft a title"}',
        expected_turn=1,
        idempotency_key="account-1:gateway-instance-1:1",
    )
    activity_output = SubagentDispatchOutput(
        output='{"text":"A deterministic title"}',
        turn_id="turn-1",
        turn_number=1,
    )
    response.input.payloads.extend(
        await pydantic_data_converter.encode([activity_input])
    )
    response.outcome.result.payloads.extend(
        await pydantic_data_converter.encode([activity_output])
    )
    temporal = MagicMock()
    temporal.namespace = "gateway"
    temporal.data_converter = pydantic_data_converter
    temporal.workflow_service.describe_activity_execution = AsyncMock(
        return_value=response
    )

    events = await _replay_external_activity_turn(
        temporal,
        source_session_id="gateway-instance-1",
        agent_id="writer",
        turn_number=1,
    )

    assert events is not None
    assert [event.event_type for event in events] == [
        "turn_started",
        "reply_delta",
        "reply",
        "turn_end",
    ]
    assert [event.offset for event in events] == [1, 2, 3, 4]
    assert events[0].data["user_message"] == (
        '{"type": "ask", "payload": {"text": "draft a title"}}'
    )
    assert events[1].data["text"] == "A deterministic title"
    request = temporal.workflow_service.describe_activity_execution.await_args.args[0]
    assert request.activity_id == "subagent-dispatch-gateway-instance-1-1"
    assert request.include_input is True
    assert request.include_outcome is True


def test_spawned_external_stream_merges_temporal_and_ui_turn_offsets() -> None:
    agent = AgentRegistration(
        agent_id="writer",
        kind="external_http",
        label="Writer",
        description="External writer",
        provider_url="http://writer",
    )
    session = SessionRecord(
        account_id="account-1",
        session_id="session-writer",
        agent_id="writer",
        provider_session_id="provider-instance-1",
        source_session_id="gateway-instance-1",
        parent_session_id="session-parent",
        created_at=123.0,
        label="Writer · spawned",
        is_spawned=True,
        has_started=True,
        current_turn=2,
    )
    turn_two = [
        SessionEvent(
            offset=index,
            event_type=event_type,
            data={
                "agent_id": "writer",
                "turn_id": "turn-2",
                "turn_number": 2,
                "timestamp": 456.0,
                "type": event_type,
            },
        )
        for index, event_type in enumerate(
            ["turn_started", "reply_delta", "reply", "turn_end"], start=1
        )
    ]
    replayed_turn_one = [
        SessionEvent(
            offset=index,
            event_type=event_type,
            data={
                "agent_id": "writer",
                "turn_id": "turn-1",
                "turn_number": 1,
                "timestamp": 123.0,
                "type": event_type,
            },
        )
        for index, event_type in enumerate(
            ["turn_started", "reply_delta", "reply", "turn_end"], start=1
        )
    ]
    client, _, _ = _app_client(
        session_override=session,
        agent_override=agent,
        session_events=turn_two,
    )

    with patch(
        "durable_tools_gateway.web._replay_external_activity_turn",
        new=AsyncMock(return_value=replayed_turn_one),
    ) as replay:
        with client:
            response = client.get("/api/attach?session_id=session-writer&from_offset=2")

    assert response.status_code == 200
    body = response.text
    assert '"resume_offset": 1' not in body
    assert '"resume_offset": 2' not in body
    for offset in range(3, 9):
        assert f'"resume_offset": {offset}' in body
    replay.assert_awaited_once()
