from __future__ import annotations

from unittest.mock import patch

from durable_tools_gateway.registry import (
    AgentRegistration,
    NexusMCPServerRegistration,
    PendingSessionEvent,
    SpawnedAgentObservation,
    SubagentInstanceRoute,
    ToolRegistryWorkflow,
    account_registry_workflow_id,
)


def test_account_workflow_ids_are_stable_isolated_and_opaque() -> None:
    first = account_registry_workflow_id("account-1")

    assert first == account_registry_workflow_id("account-1")
    assert first != account_registry_workflow_id("account-2")
    assert "account-1" not in first


def test_accounts_do_not_share_agents_sessions_or_resources() -> None:
    first = ToolRegistryWorkflow("account-1")
    second = ToolRegistryWorkflow("account-2")
    registration = AgentRegistration(
        agent_id="assistant",
        kind="harness_nexus",
        label="Assistant",
        description="Test agent",
        nexus_endpoint="assistant-endpoint",
    )

    with (
        patch("durable_tools_gateway.registry.workflow.logger"),
        patch("durable_tools_gateway.registry.workflow.uuid4", return_value="one"),
        patch("durable_tools_gateway.registry.workflow.time", return_value=123.0),
    ):
        first.register_subagent("writer", "http://writer")
        first.register_agent(registration)
        session = first.create_session("assistant")

    assert first.list_account_entries().subagent_providers == {
        "writer": "http://writer"
    }
    assert first.list_agents() == [registration]
    assert first.list_sessions() == [session]
    assert session.account_id == "account-1"
    assert second.list_account_entries().subagent_providers == {}
    assert second.list_agents() == []
    assert second.list_sessions() == []


def test_native_mcp_registration_is_discovery_metadata_only() -> None:
    registry = ToolRegistryWorkflow("account-1")
    registration = NexusMCPServerRegistration(
        name="native-demo",
        endpoint="native-demo-endpoint",
        service="demo-nexus",
    )

    assert registry.register_nexus_mcp_server(registration) == registration
    assert registry.list_account_entries().nexus_servers == {
        "native-demo": registration
    }


def test_closing_session_keeps_it_in_account_history() -> None:
    registry = ToolRegistryWorkflow("account-1")
    registry.register_agent(
        AgentRegistration(
            agent_id="assistant",
            kind="harness_nexus",
            label="Assistant",
            description="Test agent",
            nexus_endpoint="assistant-endpoint",
        )
    )
    with (
        patch("durable_tools_gateway.registry.workflow.uuid4", return_value="one"),
        patch("durable_tools_gateway.registry.workflow.time", return_value=123.0),
    ):
        session = registry.create_session("assistant")

    closed = registry.close_session(session.session_id)

    assert closed.closed
    assert registry.get_session(session.session_id) == closed


def test_external_session_events_are_offset_and_replay_based() -> None:
    registry = ToolRegistryWorkflow("account-1")
    registry.register_agent(
        AgentRegistration(
            agent_id="external",
            kind="external_http",
            label="External",
            description="HTTP proof",
            provider_url="http://external",
        )
    )
    with (
        patch("durable_tools_gateway.registry.workflow.uuid4", return_value="one"),
        patch("durable_tools_gateway.registry.workflow.time", return_value=123.0),
    ):
        session = registry.create_session("external", "provider-1")

    registry.append_session_events(
        session.session_id,
        [
            PendingSessionEvent("reply_delta", {"type": "reply_delta", "text": "hi"}),
            PendingSessionEvent("turn_end", {"type": "turn_end"}),
        ],
    )

    assert [
        event.offset for event in registry.poll_session_events(session.session_id, 0)
    ] == [
        1,
        2,
    ]
    assert [
        event.event_type
        for event in registry.poll_session_events(session.session_id, 1)
    ] == ["turn_end"]


def test_registered_spawned_agents_become_account_sessions() -> None:
    registry = ToolRegistryWorkflow("account-1")
    registry.register_agent(
        AgentRegistration(
            agent_id="parent",
            kind="harness_nexus",
            label="Parent",
            description="Parent agent",
            nexus_endpoint="parent-endpoint",
        )
    )
    registry.register_agent(
        AgentRegistration(
            agent_id="research",
            kind="harness_nexus",
            label="Research",
            description="Research child",
            nexus_endpoint="research-endpoint",
        )
    )
    with (
        patch(
            "durable_tools_gateway.registry.workflow.uuid4",
            side_effect=["parent-session", "child-session"],
        ),
        patch("durable_tools_gateway.registry.workflow.time", return_value=123.0),
        patch("durable_tools_gateway.registry.workflow.logger"),
    ):
        parent = registry.create_session("parent", is_message_queuing_enabled=True)
        children = registry.sync_spawned_agents(
            parent.session_id,
            [
                SpawnedAgentObservation(
                    subagent_id="research-a1b2c3",
                    agent_key="research",
                    provider_session_id="research-workflow",
                    next_expected_turn=3,
                )
            ],
        )

    assert len(children) == 1
    child = children[0]
    assert child.parent_session_id == parent.session_id
    assert child.provider_session_id == "research-workflow"
    assert child.source_session_id == "research-workflow"
    assert child.agent_id == "research"
    assert child.is_spawned
    assert child.has_started
    assert child.current_turn == 2
    assert child.is_message_queuing_enabled

    registry.record_spawned_agent_batch(
        parent.session_id, [], ["research-workflow"], 12
    )
    assert registry.get_session(child.session_id).closed
    assert registry.get_session(parent.session_id).discovery_offset == 12


def test_gateway_spawned_agent_resolves_to_the_provider_instance() -> None:
    registry = ToolRegistryWorkflow("account-1")
    registry.register_agent(
        AgentRegistration(
            agent_id="parent",
            kind="harness_nexus",
            label="Parent",
            description="Parent agent",
            nexus_endpoint="parent-endpoint",
        )
    )
    registry.register_agent(
        AgentRegistration(
            agent_id="writer",
            kind="external_http",
            label="Writer",
            description="HTTP child",
            provider_url="http://writer",
        )
    )
    registry.bind_subagent_instance(
        "gateway-instance",
        SubagentInstanceRoute(
            alias="writer",
            url="http://writer",
            provider_instance_id="provider-instance",
        ),
    )
    with (
        patch(
            "durable_tools_gateway.registry.workflow.uuid4",
            side_effect=["parent-session", "child-session"],
        ),
        patch("durable_tools_gateway.registry.workflow.time", return_value=123.0),
        patch("durable_tools_gateway.registry.workflow.logger"),
    ):
        parent = registry.create_session("parent")
        child = registry.sync_spawned_agents(
            parent.session_id,
            [
                SpawnedAgentObservation(
                    subagent_id="writer-a1b2c3",
                    agent_key="writer",
                    provider_session_id="gateway-instance",
                    next_expected_turn=2,
                )
            ],
        )[0]

    assert child.source_session_id == "gateway-instance"
    assert child.provider_session_id == "provider-instance"
    assert child.current_turn == 1

    assert registry.resolve_session(child.session_id) == child
    assert registry.resolve_session("gateway-instance") == child
    assert registry.resolve_session("provider-instance") == child
    assert registry.resolve_session("missing-instance") is None


def test_unregistered_spawned_agents_are_not_added_to_account() -> None:
    registry = ToolRegistryWorkflow("account-1")
    registry.register_agent(
        AgentRegistration(
            agent_id="parent",
            kind="harness_nexus",
            label="Parent",
            description="Parent agent",
            nexus_endpoint="parent-endpoint",
        )
    )
    with (
        patch("durable_tools_gateway.registry.workflow.logger"),
        patch("durable_tools_gateway.registry.workflow.uuid4", return_value="parent"),
        patch("durable_tools_gateway.registry.workflow.time", return_value=123.0),
    ):
        parent = registry.create_session("parent")
        children = registry.sync_spawned_agents(
            parent.session_id,
            [
                SpawnedAgentObservation(
                    subagent_id="unknown-1",
                    agent_key="unknown",
                    provider_session_id="unknown-workflow",
                )
            ],
        )

    assert children == []
    assert registry.list_sessions() == [parent]
