from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

from a2a.types import Message, Part, Role, SendMessageResponse, StreamResponse, Task
from google.protobuf.json_format import MessageToDict
from temporalio.api.common.v1 import Payload

from temporal_agent_harness.a2a.generated import (
    QueryOperatorInterfaceOutput,
)
from temporal_agent_harness.a2a.stream import stream_response
from temporal_agent_harness.web.tunnel_driver import (
    NexusControls,
    WebTunnelDriver,
    _decode_item,
    _frames,
    tunnel_workflow_id,
)


def _encoded(response: StreamResponse) -> str:
    return base64.b64encode(response.SerializeToString()).decode()


def _harness_response(event: dict, *, turn_id: str = "turn-1") -> str:
    envelope = {
        "agent_id": "agent-1",
        "turn_id": turn_id,
        "turn_number": 1,
        "timestamp": 1.0,
        "event": event,
    }
    payload = Payload(
        metadata={"encoding": b"json/plain"},
        data=json.dumps(envelope).encode(),
    )
    return _encoded(
        stream_response(base64.b64encode(payload.SerializeToString()).decode())
    )


def test_generic_a2a_record_reaches_web_driver_without_projection() -> None:
    response = StreamResponse(
        message=Message(
            message_id="message-1",
            task_id="task-1",
            context_id="context-1",
            role=Role.ROLE_AGENT,
            parts=[Part(text="hello")],
            metadata={"third-party": {"nested": [1, 2, 3]}},
        )
    )

    event_type, data = _decode_item({"offset": 4, "data": _encoded(response)})

    assert event_type == "a2a_stream_response"
    assert data["resume_offset"] == 5
    assert data["response"]["message"]["metadata"]["third-party"] == {
        "nested": [1, 2, 3]
    }


def test_web_driver_coalesces_text_but_preserves_rich_harness_events() -> None:
    items = [
        {
            "offset": 0,
            "data": _harness_response({"type": "reply_delta", "text": "hel"}),
        },
        {"offset": 1, "data": _harness_response({"type": "reply_delta", "text": "lo"})},
        {
            "offset": 2,
            "data": _harness_response(
                {"type": "tool_start", "tool_id": "tool-1", "tool_name": "search"}
            ),
        },
    ]

    frames = _frames(items)

    assert frames[0][0] == "reply_delta"
    assert frames[0][1]["text"] == "hello"
    assert frames[0][1]["resume_offset"] == 2
    assert frames[1][0] == "tool_start"
    assert frames[1][1]["tool_id"] == "tool-1"


async def test_standalone_control_operation_has_an_explicit_id() -> None:
    control_client = MagicMock()
    control_client.execute_operation = AsyncMock(
        return_value=QueryOperatorInterfaceOutput(commands=[])
    )
    temporal = MagicMock()
    temporal.create_nexus_client.side_effect = [MagicMock(), control_client]
    controls = NexusControls(temporal, "agent-endpoint")

    with patch(
        "temporal_agent_harness.web.tunnel_driver.uuid.uuid4",
        return_value="operation-1",
    ):
        assert await controls.operator_interface("session-1") == []

    assert control_client.execute_operation.await_args.kwargs["id"] == (
        "web-query-operator-interface-operation-1"
    )


async def test_send_message_is_a_standalone_a2a_operation() -> None:
    a2a_client = MagicMock()
    a2a_client.execute_operation = AsyncMock(
        return_value=SendMessageResponse(
            task=Task(
                id="session-1",
                metadata={
                    "temporal.io/turn-number": 3,
                    "temporal.io/turn-id": "turn-3",
                    "temporal.io/accepted-offset": 41,
                    "temporal.io/pending": False,
                },
            )
        )
    )
    temporal = MagicMock()
    temporal.create_nexus_client.side_effect = [a2a_client, MagicMock()]
    controls = NexusControls(temporal, "agent-endpoint")

    with patch(
        "temporal_agent_harness.web.tunnel_driver.uuid.uuid4",
        return_value="operation-1",
    ):
        accepted = await controls.send_message(
            "session-1",
            message_type="ask",
            payload={"text": "hello"},
            expected_turn=3,
        )

    call = a2a_client.execute_operation.await_args
    assert call.kwargs["id"] == "web-send-message-operation-1"
    assert call.args[1].message.task_id == "session-1"
    assert MessageToDict(call.args[1].metadata)["expected_turn"] == 3
    assert accepted == {
        "turnNumber": 3,
        "turnId": "turn-3",
        "streamHeadOffset": 41,
        "pending": False,
    }


async def test_mount_starts_a_workflow_for_exactly_one_turn() -> None:
    temporal = MagicMock()
    temporal.execute_update_with_start_workflow = AsyncMock(return_value={})
    driver = WebTunnelDriver(
        temporal, task_queue="tunnel-queue", nexus_endpoint="agent-endpoint"
    )

    with patch(
        "temporal_agent_harness.web.tunnel_driver.uuid.uuid4",
        return_value="subscriber-1",
    ):
        mounted = await driver.mount(
            "session-1", cursor=41, mode="participant", turn_number=3
        )

    assert mounted == ("web-subscriber-1", 3)
    start = temporal.execute_update_with_start_workflow.await_args.kwargs[
        "start_workflow_operation"
    ]
    start_input = start._start_workflow_input
    assert start_input.id == tunnel_workflow_id("session-1", 3)
    assert start_input.args[0]["turnNumber"] == 3
    assert start_input.args[0]["fromOffset"] == 41
    assert start_input.args[0]["knownComplete"] is False


async def test_mount_of_idle_agent_drains_history_without_a_long_poll() -> None:
    temporal = MagicMock()
    temporal.execute_update_with_start_workflow = AsyncMock(return_value={})
    driver = WebTunnelDriver(
        temporal, task_queue="tunnel-queue", nexus_endpoint="agent-endpoint"
    )
    controls = MagicMock()
    controls.status = AsyncMock(
        return_value={
            "current_turn": 2,
            "turn_active": False,
            "pending_approvals": [],
            "pending_callbacks": [],
        }
    )

    with (
        patch.object(driver, "controls", return_value=controls),
        patch(
            "temporal_agent_harness.web.tunnel_driver.uuid.uuid4",
            return_value="subscriber-1",
        ),
    ):
        mounted = await driver.mount("session-1", cursor=20)

    assert mounted == ("web-subscriber-1", 2)
    start = temporal.execute_update_with_start_workflow.await_args.kwargs[
        "start_workflow_operation"
    ]
    assert start._start_workflow_input.args[0]["knownComplete"] is True
