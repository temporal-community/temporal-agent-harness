# ABOUTME: Tests for the inbound A2A gateway — translate.py's pure conversions, and the FastAPI
# app's JSON-RPC dispatch against a fake standalone Nexus client (no real server needed).

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from temporalio.api.common.v1 import Payload as CommonPayload

from temporal_agent_harness.harness.agent_protocol import AgentEvent, AgentReply, TurnEnded, TurnStarted

from subagents.registry.agent_registry_service import AgentElement, HandlerElement
from subagents.transport import (
    Message as InternalMessage,
    PollTaskUpdatesOutput,
    StreamItem,
    SubagentService,
    Task as InternalTask,
    TaskStatus as InternalTaskStatus,
)

from subagent_gateway import a2a_types as a2a
from subagent_gateway.app import create_app
from subagent_gateway.config import GatewayConfig
from subagent_gateway.translate import (
    agent_event_to_a2a_stream_event,
    incoming_message_to_internal,
    internal_task_to_a2a,
)

# ---------------------------------------------------------------------------
# translate.py — pure conversions
# ---------------------------------------------------------------------------


def test_internal_task_to_a2a_drops_correlation_only_message():
    task = InternalTask(
        id="t1",
        context_id="t1",
        status=InternalTaskStatus(state="working", message=InternalMessage(role="agent", parts=[], task_id="t1", message_id="turn-abc")),
    )
    out = internal_task_to_a2a(task)
    assert out.id == "t1"
    assert out.status.state == "working"
    assert out.status.message is None  # correlation stub, not real content — never surfaced


def test_incoming_message_with_text_part_wraps_as_text_field():
    msg = a2a.Message(role="user", parts=[a2a.TextPart(text="hello")], messageId="m1")
    internal = incoming_message_to_internal(msg, task_id="t1", default_handler="echo")
    assert internal.task_id == "t1"
    assert internal.parts[0].kind == "data"
    payload = json.loads(internal.parts[0].data)
    assert payload == {"handler": "echo", "input": {"text": "hello"}}


def test_incoming_message_with_multiple_text_parts_joins_with_newline():
    msg = a2a.Message(
        role="user",
        parts=[a2a.TextPart(text="line1"), a2a.TextPart(text="line2")],
        messageId="m1",
    )
    internal = incoming_message_to_internal(msg, task_id="t1", default_handler="echo")
    payload = json.loads(internal.parts[0].data)
    assert payload["input"]["text"] == "line1\nline2"


def test_incoming_message_with_data_part_uses_it_verbatim():
    msg = a2a.Message(
        role="user",
        parts=[a2a.DataPart(data={"text": "structured", "extra": 1})],
        messageId="m1",
    )
    internal = incoming_message_to_internal(msg, task_id="t1", default_handler="echo")
    payload = json.loads(internal.parts[0].data)
    assert payload == {"handler": "echo", "input": {"text": "structured", "extra": 1}}


def _ev(payload: Any) -> AgentEvent:
    return AgentEvent(agent_id="a1", turn_id="turn-1", turn_number=1, timestamp=0.0, event=payload)


def test_agent_event_translation_covers_lifecycle_and_drops_unmapped():
    started = agent_event_to_a2a_stream_event(
        _ev(TurnStarted(user_message="hi")), task_id="t1", context_id="t1"
    )
    assert isinstance(started, a2a.TaskStatusUpdateEvent)
    assert started.status.state == "working" and not started.final

    reply = agent_event_to_a2a_stream_event(
        _ev(AgentReply(output={"text": "HELLO"})), task_id="t1", context_id="t1"
    )
    assert isinstance(reply, a2a.TaskArtifactUpdateEvent)
    assert reply.artifact.parts[0].data == {"text": "HELLO"}

    ended = agent_event_to_a2a_stream_event(_ev(TurnEnded()), task_id="t1", context_id="t1")
    assert isinstance(ended, a2a.TaskStatusUpdateEvent)
    assert ended.status.state == "completed" and ended.final


# ---------------------------------------------------------------------------
# app.py — JSON-RPC dispatch, against a fake standalone Nexus client
# ---------------------------------------------------------------------------


def _encode_event(event: AgentEvent, offset: int) -> StreamItem:
    payload = CommonPayload(
        metadata={"encoding": b"json/plain"},
        data=json.dumps(event.model_dump(mode="json")).encode(),
    )
    return StreamItem(topic="turn_events", data=base64.b64encode(payload.SerializeToString()).decode(), offset=offset)


class _FakeNexusOpClient:
    def __init__(self, queues: dict[str, list[Any]]) -> None:
        self._queues = queues

    async def execute_operation(self, operation: Any, input: Any, *, id: str, schedule_to_close_timeout: Any):
        queue = self._queues[operation.name]  # nexusrpc.Operation isn't hashable; key by name
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        return value(input) if callable(value) else value


class _FakeClient:
    def __init__(self, queues: dict[str, list[Any]]) -> None:
        self._queues = queues

    def create_nexus_client(self, *, service: Any, endpoint: str) -> _FakeNexusOpClient:
        assert service is SubagentService
        return _FakeNexusOpClient(self._queues)


def _agent() -> AgentElement:
    return AgentElement(
        agent_key="echo",
        endpoint="echo-agent-nexus-endpoint",
        handlers=[HandlerElement(name="echo", description="Echoes text back.", parameters={}, output={})],
        description="Echo agent",
    )


def _make_app(queues: dict[Any, list[Any]]) -> httpx.ASGITransport:
    client = _FakeClient(queues)
    config = GatewayConfig(registry_endpoint="agent-registry-endpoint", agent_key="echo", default_handler="echo")
    app = create_app(client=client, config=config, agent=_agent(), default_handler="echo")
    return httpx.ASGITransport(app=app)


@pytest.mark.asyncio
async def test_agent_card_endpoint():
    transport = _make_app(queues={})
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "echo"
    assert card["skills"][0]["id"] == "echo"


@pytest.mark.asyncio
async def test_message_send_non_blocking_returns_working_task():
    sent_task = InternalTask(id="t1", context_id="t1", status=InternalTaskStatus(state="working"), stream_head_offset=0, turn_number=1)
    transport = _make_app(queues={SubagentService.send_message.name: [sent_task]})
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}], "messageId": "m1", "taskId": "t1"}},
            },
        )
    body = resp.json()
    assert body["result"]["id"] == "t1"
    assert body["result"]["status"]["state"] == "working"


@pytest.mark.asyncio
async def test_message_send_blocking_polls_to_completion():
    sent_task = InternalTask(id="t1", context_id="t1", status=InternalTaskStatus(state="working"), stream_head_offset=0, turn_number=1)
    events = [
        _encode_event(_ev(TurnStarted(user_message="hi")), 0),
        _encode_event(_ev(AgentReply(output={"text": "HI"})), 1),
        _encode_event(_ev(TurnEnded()), 2),
    ]
    poll_result = PollTaskUpdatesOutput(items=events, next_offset=3, more_ready=False, closed=False)
    final_task = InternalTask(id="t1", context_id="t1", status=InternalTaskStatus(state="completed"))
    transport = _make_app(
        queues={
            SubagentService.send_message.name: [sent_task],
            SubagentService.poll_task_updates.name: [poll_result],
            SubagentService.get_task.name: [final_task],
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}], "messageId": "m1", "taskId": "t1"},
                    "configuration": {"blocking": True},
                },
            },
        )
    body = resp.json()
    assert body["result"]["status"]["state"] == "completed"


@pytest.mark.asyncio
async def test_tasks_get_and_cancel():
    task = InternalTask(id="t1", context_id="t1", status=InternalTaskStatus(state="completed"))
    canceled = InternalTask(id="t1", context_id="t1", status=InternalTaskStatus(state="canceled"))
    transport = _make_app(
        queues={SubagentService.get_task.name: [task], SubagentService.cancel_task.name: [canceled]}
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        get_resp = await http.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "t1"}})
        cancel_resp = await http.post("/", json={"jsonrpc": "2.0", "id": 2, "method": "tasks/cancel", "params": {"id": "t1"}})
    assert get_resp.json()["result"]["status"]["state"] == "completed"
    assert cancel_resp.json()["result"]["status"]["state"] == "canceled"


@pytest.mark.asyncio
async def test_unknown_method_returns_json_rpc_error():
    transport = _make_app(queues={})
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        resp = await http.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "bogus/method", "params": {}})
    body = resp.json()
    assert body["error"]["code"] == a2a.METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_message_stream_emits_sse_frames_and_terminates_on_final():
    sent_task = InternalTask(id="t1", context_id="t1", status=InternalTaskStatus(state="working"), stream_head_offset=0, turn_number=1)
    events = [
        _encode_event(_ev(TurnStarted(user_message="hi")), 0),
        _encode_event(_ev(AgentReply(output={"text": "HI"})), 1),
        _encode_event(_ev(TurnEnded()), 2),
    ]
    poll_result = PollTaskUpdatesOutput(items=events, next_offset=3, more_ready=False, closed=False)
    transport = _make_app(
        queues={
            SubagentService.send_message.name: [sent_task],
            SubagentService.poll_task_updates.name: [poll_result],
        }
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        async with http.stream(
            "POST",
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "message/stream",
                "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}], "messageId": "m1", "taskId": "t1"}},
            },
        ) as resp:
            lines = [line async for line in resp.aiter_lines() if line.startswith("data: ")]
    frames = [json.loads(line[len("data: ") :]) for line in lines]
    kinds = [f["result"]["kind"] for f in frames]
    assert kinds == ["status-update", "artifact-update", "status-update"]
    assert frames[-1]["result"]["final"] is True
    assert all(f["id"] == 7 for f in frames)
