from __future__ import annotations

from unittest.mock import patch

from durable_tools_gateway.registry import (
    AgentRegistration,
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
