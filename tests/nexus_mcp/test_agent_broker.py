from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

from durable_tools_gateway.agent_broker import (
    AgentActionInput,
    AgentActionWorkflow,
    AgentAttachInput,
    AgentAttachWorkflow,
    PublishBatchInput,
    PublishBatchResult,
    event_broker,
    publish_agent_events,
)
from durable_tools_gateway.registry import SpawnedAgentObservation
from durable_tools_gateway.registry_service_handler import (
    SubagentDispatchOutput,
    SubagentStartResult,
)
from temporalio.api.common.v1 import Payload

from temporal_agent_harness.nexus_agent_adapter.generated import (
    PollMessagesOutput,
    SendMessageOutput,
    StreamItem,
    SubagentInfo,
)


def _stream_item(offset: int = 4, text: str = "hello") -> StreamItem:
    envelope = {
        "agent_id": "agent-1",
        "turn_id": "turn-1",
        "turn_number": 1,
        "timestamp": 123.0,
        "event": {"type": "reply_delta", "text": text},
    }
    payload = Payload(data=json.dumps(envelope).encode())
    return StreamItem(
        topic="turn_events",
        data=base64.b64encode(payload.SerializeToString()).decode(),
        offset=offset,
    )


def _subagent_stream_item(event_type: str, offset: int) -> StreamItem:
    envelope = {
        "agent_id": "parent-1",
        "turn_id": "turn-1",
        "turn_number": 1,
        "timestamp": 123.0,
        "event": {
            "type": event_type,
            "subagent_id": "research-a1b2c3",
            "agent_key": "research",
            "workflow_id": "research-workflow",
        },
    }
    payload = Payload(data=json.dumps(envelope).encode())
    return StreamItem(
        topic="turn_events",
        data=base64.b64encode(payload.SerializeToString()).decode(),
        offset=offset,
    )


async def test_publish_activity_decodes_the_harness_stream_envelope() -> None:
    queue = event_broker.subscribe("stream-1")
    try:
        result = await publish_agent_events(
            PublishBatchInput(stream_id="stream-1", items=[_stream_item()])
        )
        frame = await queue.get()
    finally:
        event_broker.unsubscribe("stream-1", queue)

    assert not result.saw_terminal_event
    assert frame is not None
    assert b"event: reply_delta" in frame
    assert b'"resume_offset": 5' in frame


async def test_publish_activity_coalesces_adjacent_reply_deltas() -> None:
    queue = event_broker.subscribe("stream-coalesced")
    try:
        await publish_agent_events(
            PublishBatchInput(
                stream_id="stream-coalesced",
                items=[_stream_item(4, "hel"), _stream_item(5, "lo")],
            )
        )
        frame = await queue.get()
    finally:
        event_broker.unsubscribe("stream-coalesced", queue)

    assert frame is not None
    assert b'"text": "hello"' in frame
    assert b'"resume_offset": 6' in frame
    assert queue.empty()


async def test_publish_activity_projects_spawned_agent_lifecycle() -> None:
    result = await publish_agent_events(
        PublishBatchInput(
            stream_id="no-browser",
            items=[
                _subagent_stream_item("subagent_started", 1),
                _subagent_stream_item("subagent_stopped", 2),
            ],
        )
    )

    assert result.spawned_agents[0].agent_key == "research"
    assert result.spawned_agents[0].provider_session_id == "research-workflow"
    assert result.stopped_provider_session_ids == ["research-workflow"]


@patch("durable_tools_gateway.agent_broker.workflow.get_external_workflow_handle")
async def test_attach_records_one_registry_batch_with_its_discovery_cursor(
    mock_external_handle: MagicMock,
) -> None:
    registry = MagicMock()
    registry.signal = AsyncMock()
    mock_external_handle.return_value = registry
    observation = SpawnedAgentObservation(
        subagent_id="research-a1b2c3",
        agent_key="research",
        provider_session_id="research-workflow",
    )

    await AgentAttachWorkflow()._record_spawned_agents(
        AgentAttachInput(
            nexus_endpoint="agent-endpoint",
            session_id="provider-session",
            stream_id="browser-stream",
            registry_workflow_id="account-registry",
            account_session_id="account-session",
        ),
        PublishBatchResult(spawned_agents=[observation]),
        17,
    )

    registry.signal.assert_awaited_once_with(
        "record_spawned_agent_batch",
        args=["account-session", [observation], [], 17],
    )


@patch("durable_tools_gateway.agent_broker.workflow.create_nexus_client")
async def test_native_send_is_one_nexus_action(mock_create_client: MagicMock) -> None:
    nexus = MagicMock()
    nexus.execute_operation = AsyncMock(
        return_value=SendMessageOutput(
            turn_number=2,
            turn_id="turn-2",
            stream_head_offset=8,
            pending=False,
        )
    )
    mock_create_client.return_value = nexus

    result = await AgentActionWorkflow().run(
        AgentActionInput(
            action="send",
            session_id="provider-session",
            nexus_endpoint="agent-endpoint",
            values={
                "msg_type": "ask",
                "payload": {"text": "hello"},
                "expected_turn": 2,
            },
        )
    )

    sent = nexus.execute_operation.await_args.args[1]
    assert sent.session_id == "provider-session"
    assert sent.expected_turn == 2
    assert result == {
        "turn_number": 2,
        "turn_id": "turn-2",
        "stream_head_offset": 8,
        "pending": False,
    }


@patch("durable_tools_gateway.agent_broker.workflow.create_nexus_client")
async def test_native_send_can_defer_turn_reconciliation_to_agent_service(
    mock_create_client: MagicMock,
) -> None:
    nexus = MagicMock()
    nexus.execute_operation = AsyncMock(
        return_value=SendMessageOutput(
            turn_number=3,
            turn_id="turn-3",
            stream_head_offset=9,
            pending=False,
        )
    )
    mock_create_client.return_value = nexus

    await AgentActionWorkflow().run(
        AgentActionInput(
            action="send",
            session_id="provider-session",
            nexus_endpoint="agent-endpoint",
            values={"msg_type": "ask", "payload": {"text": "hello"}},
        )
    )

    sent = nexus.execute_operation.await_args.args[1]
    assert sent.expected_turn is None


@patch("durable_tools_gateway.agent_broker.workflow.create_nexus_client")
async def test_subagent_discovery_reads_missed_lifecycle_and_live_status(
    mock_create_client: MagicMock,
) -> None:
    nexus = MagicMock()
    status = MagicMock(
        subagents=[
            SubagentInfo(
                subagent_id="research-a1b2c3",
                agent_key="research",
                workflow_id="research-workflow",
                next_expected_turn=3,
            )
        ]
    )
    nexus.execute_operation = AsyncMock(
        side_effect=[
            PollMessagesOutput(
                items=[_subagent_stream_item("subagent_started", 4)],
                next_offset=5,
                more_ready=False,
                closed=False,
            ),
            status,
        ]
    )
    mock_create_client.return_value = nexus

    result = await AgentActionWorkflow().run(
        AgentActionInput(
            action="discover_subagents",
            session_id="provider-session",
            nexus_endpoint="agent-endpoint",
            values={"cursor": 2},
        )
    )

    assert result["next_offset"] == 5
    assert result["spawned"][0]["workflow_id"] == "research-workflow"
    assert result["active"][0]["next_expected_turn"] == 3


@patch("durable_tools_gateway.agent_broker.workflow.execute_activity")
async def test_external_agent_uses_the_minimal_http_provider_protocol(
    mock_execute_activity: AsyncMock,
) -> None:
    mock_execute_activity.side_effect = [
        SubagentStartResult(instance_id="provider-1"),
        SubagentDispatchOutput(
            output='{"text":"hello"}', turn_id="turn-1", turn_number=1
        ),
    ]
    action = AgentActionWorkflow()

    started = await action.run(
        AgentActionInput(
            action="start",
            session_id="",
            provider_url="http://provider",
            values={"idempotency_key": "start-1"},
        )
    )
    sent = await action.run(
        AgentActionInput(
            action="send",
            session_id="provider-1",
            provider_url="http://provider",
            values={
                "msg_type": "ask",
                "payload": {"text": "hi"},
                "expected_turn": 1,
                "idempotency_key": "turn-1",
            },
        )
    )

    assert started == {"instance_id": "provider-1"}
    assert sent["turn_number"] == 1
    assert mock_execute_activity.await_count == 2


@patch("durable_tools_gateway.agent_broker.workflow.execute_activity")
@patch("durable_tools_gateway.agent_broker.workflow.create_nexus_client")
async def test_attach_stops_after_an_empty_poll_when_agent_is_idle(
    mock_create_client: MagicMock,
    mock_execute_activity: AsyncMock,
) -> None:
    nexus = MagicMock()
    nexus.execute_operation = AsyncMock(
        side_effect=[
            PollMessagesOutput(items=[], next_offset=3, more_ready=False, closed=False),
            MagicMock(
                turn_active=False,
                pending_turns=[],
                pending_approvals=[],
                pending_callbacks=[],
            ),
        ]
    )
    mock_create_client.return_value = nexus
    mock_execute_activity.side_effect = [
        PublishBatchResult(saw_terminal_event=False),
        PublishBatchResult(saw_terminal_event=False),
    ]

    await AgentAttachWorkflow().run(
        AgentAttachInput(
            nexus_endpoint="agent-endpoint",
            session_id="provider-session",
            stream_id="browser-stream",
            from_offset=3,
        )
    )

    assert nexus.execute_operation.await_count == 2
    close_batch = mock_execute_activity.await_args_list[-1].args[1]
    assert close_batch.close


@patch("durable_tools_gateway.agent_broker.workflow.execute_activity")
@patch("durable_tools_gateway.agent_broker.workflow.create_nexus_client")
async def test_attach_stays_open_until_all_pending_approvals_resolve(
    mock_create_client: MagicMock,
    mock_execute_activity: AsyncMock,
) -> None:
    nexus = MagicMock()
    nexus.execute_operation = AsyncMock(
        side_effect=[
            PollMessagesOutput(
                items=[_stream_item()],
                next_offset=5,
                more_ready=False,
                closed=False,
            ),
            MagicMock(
                turn_active=False,
                pending_turns=[],
                pending_approvals=[MagicMock()],
                pending_callbacks=[],
            ),
            PollMessagesOutput(
                items=[], next_offset=5, more_ready=False, closed=False
            ),
            MagicMock(
                turn_active=False,
                pending_turns=[],
                pending_approvals=[],
                pending_callbacks=[],
            ),
        ]
    )
    mock_create_client.return_value = nexus
    mock_execute_activity.side_effect = [
        PublishBatchResult(saw_terminal_event=True),
        PublishBatchResult(saw_terminal_event=False),
        PublishBatchResult(saw_terminal_event=False),
    ]

    await AgentAttachWorkflow().run(
        AgentAttachInput(
            nexus_endpoint="agent-endpoint",
            session_id="provider-session",
            stream_id="browser-stream",
            from_offset=4,
        )
    )

    assert nexus.execute_operation.await_count == 4
    close_batch = mock_execute_activity.await_args_list[-1].args[1]
    assert close_batch.close


@patch("durable_tools_gateway.agent_broker.workflow.execute_activity")
@patch("durable_tools_gateway.agent_broker.workflow.create_nexus_client")
async def test_attach_publishes_final_items_before_closing(
    mock_create_client: MagicMock,
    mock_execute_activity: AsyncMock,
) -> None:
    nexus = MagicMock()
    nexus.execute_operation = AsyncMock(
        return_value=PollMessagesOutput(
            items=[_stream_item()], next_offset=5, more_ready=False, closed=True
        )
    )
    mock_create_client.return_value = nexus
    mock_execute_activity.return_value = PublishBatchResult()

    await AgentAttachWorkflow().run(
        AgentAttachInput(
            nexus_endpoint="agent-endpoint",
            session_id="provider-session",
            stream_id="browser-stream",
        )
    )

    batch = mock_execute_activity.await_args.args[1]
    assert batch.items == [_stream_item()]
    assert batch.close
