from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

from nexus_mcp.durable_tools_gateway.agent_broker import (
    AgentDiscoveryInput,
    AgentDiscoveryWorkflow,
    ExternalAgentActionInput,
    execute_external_agent_action,
)
from nexus_mcp.durable_tools_gateway.registry_service_handler import (
    SubagentDispatchOutput,
    SubagentStartResult,
)
from temporalio.api.common.v1 import Payload

from nexus_a2a import (
    SubscribeToTaskItem,
    SubscribeToTaskOutput,
)
from temporal_agent_harness.a2a.generated import (
    SubagentInfo,
)
from temporal_agent_harness.a2a.stream import stream_response


def _subagent_stream_item(event_type: str, offset: int) -> SubscribeToTaskItem:
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
    encoded = base64.b64encode(payload.SerializeToString()).decode()
    return SubscribeToTaskItem(
        data=base64.b64encode(stream_response(encoded).SerializeToString()).decode(),
        offset=offset,
    )


@patch("nexus_mcp.durable_tools_gateway.agent_broker.workflow.create_nexus_client")
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
            SubscribeToTaskOutput(
                items=[_subagent_stream_item("subagent_started", 4)],
                next_cursor=5,
                more_ready=False,
                closed=False,
            ),
            status,
        ]
    )
    mock_create_client.return_value = nexus

    result = await AgentDiscoveryWorkflow().run(
        AgentDiscoveryInput(
            session_id="provider-session",
            nexus_endpoint="agent-endpoint",
            cursor=2,
        )
    )

    assert result["next_offset"] == 5
    assert result["spawned"][0]["workflow_id"] == "research-workflow"
    assert result["active"][0]["next_expected_turn"] == 3


async def test_external_agent_uses_standalone_activities() -> None:
    temporal = MagicMock()
    temporal.execute_activity = AsyncMock(
        side_effect=[
            SubagentStartResult(instance_id="provider-1"),
            SubagentDispatchOutput(
                output='{"text":"hello"}', turn_id="turn-1", turn_number=1
            ),
        ]
    )

    started = await execute_external_agent_action(
        temporal,
        ExternalAgentActionInput(
            action="start",
            session_id="",
            provider_url="http://provider",
            values={"idempotency_key": "start-1"},
        ),
        execution_id="broker-external-start",
    )
    sent = await execute_external_agent_action(
        temporal,
        ExternalAgentActionInput(
            action="send",
            session_id="provider-1",
            provider_url="http://provider",
            values={
                "msg_type": "ask",
                "payload": {"text": "hi"},
                "expected_turn": 1,
                "idempotency_key": "turn-1",
            },
        ),
        execution_id="broker-external-send",
    )

    assert started == {"instance_id": "provider-1"}
    assert sent["turn_number"] == 1
    assert temporal.execute_activity.await_count == 2
    assert temporal.execute_activity.await_args_list[0].kwargs["id"] == (
        "broker-external-start"
    )
