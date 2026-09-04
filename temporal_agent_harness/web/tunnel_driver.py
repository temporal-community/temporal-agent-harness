"""Browser/SSE driver for the shared Go UI tunnel workflow."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from a2a.types import (
    CancelTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
)
from google.protobuf.json_format import MessageToDict
from nexus_a2a import A2AService
from temporalio.api.common.v1 import Payload
from temporalio.client import Client, WithStartWorkflowOperation
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.a2a.generated import (
    ApproveToolCallInput,
    ExecuteOperatorCommandInput,
    HarnessControlService,
    ProvideCallbackResultInput,
    ProvideCallbackResultInputResult,
    QuerySessionInput,
)
from temporal_agent_harness.a2a.stream import HARNESS_EVENT_METADATA_KEY

TUNNEL_WORKFLOW_NAME = "UIAgentTunnelWorkflow"
REGISTER_UPDATE = "registerSubscriber"
READ_UPDATE = "readEvents"
UNREGISTER_SIGNAL = "unregisterSubscriber"


def tunnel_workflow_id(session_id: str, turn_number: int) -> str:
    return f"ui-tunnel-{session_id}-turn-{turn_number}"


class WebTunnelDriver:
    """Mount the browser as one independently paced tunnel subscriber."""

    def __init__(
        self,
        temporal: Client,
        *,
        task_queue: str,
        nexus_endpoint: str,
    ) -> None:
        self.temporal = temporal
        self.task_queue = task_queue
        self.nexus_endpoint = nexus_endpoint

    def _start(
        self,
        session_id: str,
        turn_number: int,
        from_offset: int,
        known_complete: bool,
    ) -> WithStartWorkflowOperation[Any, Any]:
        return WithStartWorkflowOperation(
            TUNNEL_WORKFLOW_NAME,
            {
                "sessionId": session_id,
                "nexusEndpoint": self.nexus_endpoint,
                "turnNumber": turn_number,
                "fromOffset": from_offset,
                "knownComplete": known_complete,
            },
            id=tunnel_workflow_id(session_id, turn_number),
            task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )

    async def mount(
        self,
        session_id: str,
        *,
        cursor: int,
        mode: str = "observer",
        turn_number: int | None = None,
        known_complete: bool = False,
    ) -> tuple[str, int] | None:
        if turn_number is None:
            status = await self.controls().status(session_id)
            turn_number = int(status.get("current_turn", 0))
            known_complete = (
                not status.get("turn_active", False)
                and not status.get("pending_approvals")
                and not status.get("pending_callbacks")
            )
        if turn_number <= 0:
            return None
        subscriber_id = f"web-{uuid.uuid4()}"
        await self.temporal.execute_update_with_start_workflow(
            REGISTER_UPDATE,
            {
                "subscriber": {
                    "id": subscriber_id,
                    "mode": mode,
                    "cursor": cursor,
                }
            },
            start_workflow_operation=self._start(
                session_id, turn_number, cursor, known_complete
            ),
            id=f"mount-{subscriber_id}",
        )
        return subscriber_id, turn_number

    async def send_and_mount(
        self,
        session_id: str,
        *,
        message_type: str,
        payload: dict[str, Any],
        expected_turn: int,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        result = await self.controls().send_message(
            session_id,
            message_type=message_type,
            payload=payload,
            expected_turn=expected_turn,
            metadata=metadata,
        )
        mounted = await self.mount(
            session_id,
            cursor=int(result.get("streamHeadOffset", 0)),
            mode="participant",
            turn_number=int(result["turnNumber"]),
        )
        if mounted is None:  # pragma: no cover - accepted turns are always positive
            raise RuntimeError("A2A SendMessage accepted no mountable turn")
        subscriber_id, _ = mounted
        return subscriber_id, result

    async def send(
        self,
        session_id: str,
        *,
        message_type: str,
        payload: dict[str, Any],
        expected_turn: int,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.controls().send_message(
            session_id,
            message_type=message_type,
            payload=payload,
            expected_turn=expected_turn,
            metadata=metadata,
        )

    async def stream(
        self,
        session_id: str,
        turn_number: int,
        subscriber_id: str,
        *,
        cursor: int,
        stop_at_turn_end: bool,
        on_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AsyncIterator[bytes]:
        handle = self.temporal.get_workflow_handle(
            tunnel_workflow_id(session_id, turn_number)
        )
        try:
            while True:
                page = await handle.execute_update(
                    READ_UPDATE,
                    {
                        "subscriberId": subscriber_id,
                        "cursor": cursor,
                        "maximumItems": 256,
                        "waitSeconds": 20.0,
                    },
                    result_type=dict,
                )
                cursor = int(page.get("nextCursor", cursor))
                frames = _frames(page.get("items") or [])
                for event_type, data in frames:
                    if on_event is not None:
                        await on_event(event_type, data)
                    yield _sse(event_type, data)
                    if stop_at_turn_end and event_type in {
                        "turn_end",
                        "operator_command_completed",
                        "operator_command_failed",
                    }:
                        return
                if page.get("closed"):
                    return
                if not frames:
                    yield b": keep-alive\n\n"
        finally:
            try:
                await handle.signal(UNREGISTER_SIGNAL, subscriber_id)
            except RPCError as exc:
                # Reading the terminal page unregisters the pull subscriber and lets
                # the bounded turn tunnel complete before this generator unwinds.
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise

    def controls(self) -> NexusControls:
        return NexusControls(self.temporal, self.nexus_endpoint)


class NexusControls:
    def __init__(self, temporal: Client, endpoint: str) -> None:
        self._a2a = temporal.create_nexus_client(service=A2AService, endpoint=endpoint)
        self._control = temporal.create_nexus_client(
            service=HarnessControlService, endpoint=endpoint
        )

    @staticmethod
    def _operation_id(kind: str) -> str:
        """Allocate one stable ID for the lifetime of a standalone Nexus call."""
        return f"web-{kind}-{uuid.uuid4()}"

    async def send_message(
        self,
        session_id: str,
        *,
        message_type: str,
        payload: dict[str, Any],
        expected_turn: int,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation_id = self._operation_id("send-message")
        request_metadata = dict(metadata or {})
        if expected_turn > 0:
            request_metadata["expected_turn"] = expected_turn
        result = await self._a2a.execute_operation(
            A2AService.send_message,
            SendMessageRequest(
                message=Message(
                    message_id=operation_id,
                    task_id=session_id,
                    context_id=session_id,
                    role=Role.ROLE_USER,
                    parts=[Part(text=str(payload.get("text", json.dumps(payload))))],
                    metadata={
                        "temporal.io/message-type": message_type,
                        "temporal.io/payload": json.dumps(payload),
                    },
                ),
                metadata=request_metadata,
            ),
            id=operation_id,
            schedule_to_close_timeout=timedelta(seconds=90),
        )
        task_metadata = MessageToDict(
            result.task.metadata, preserving_proto_field_name=True
        )
        return {
            "turnNumber": int(task_metadata["temporal.io/turn-number"]),
            "turnId": str(task_metadata["temporal.io/turn-id"]),
            "streamHeadOffset": int(
                task_metadata["temporal.io/accepted-offset"]
            ),
            "pending": bool(task_metadata.get("temporal.io/pending", False)),
        }

    async def status(self, session_id: str) -> dict[str, Any]:
        result = await self._control.execute_operation(
            HarnessControlService.query_agent_status,
            QuerySessionInput(session_id=session_id),
            id=self._operation_id("query-agent-status"),
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        return asdict(result)

    async def agent_interface(self, session_id: str) -> list[dict[str, Any]]:
        result = await self._control.execute_operation(
            HarnessControlService.query_agent_interface,
            QuerySessionInput(session_id=session_id),
            id=self._operation_id("query-agent-interface"),
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        return [
            {
                "name": item.name,
                "description": item.description,
                "parameters": json.loads(item.parameters),
                "output": json.loads(item.output),
            }
            for item in result.handlers
        ]

    async def operator_interface(self, session_id: str) -> list[dict[str, Any]]:
        result = await self._control.execute_operation(
            HarnessControlService.query_operator_interface,
            QuerySessionInput(session_id=session_id),
            id=self._operation_id("query-operator-interface"),
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        return [asdict(item) for item in result.commands]

    async def operator_command(
        self, session_id: str, name: str, arg: str | None
    ) -> dict[str, Any]:
        result = await self._control.execute_operation(
            HarnessControlService.execute_operator_command,
            ExecuteOperatorCommandInput(session_id=session_id, name=name, arg=arg),
            id=self._operation_id("execute-operator-command"),
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        return asdict(result)

    async def approve(
        self,
        session_id: str,
        tool_id: str,
        *,
        approved: bool,
        reason: str | None,
        remember: bool,
    ) -> dict[str, Any]:
        result = await self._control.execute_operation(
            HarnessControlService.approve_tool_call,
            ApproveToolCallInput(
                session_id=session_id,
                tool_id=tool_id,
                approved=approved,
                reason=reason,
                remember=remember,
            ),
            id=self._operation_id("approve-tool-call"),
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        return asdict(result)

    async def callback(
        self,
        session_id: str,
        tool_id: str,
        *,
        result: Any,
        error: str | None,
    ) -> dict[str, Any]:
        output = await self._control.execute_operation(
            HarnessControlService.provide_callback_result,
            ProvideCallbackResultInput(
                session_id=session_id,
                tool_id=tool_id,
                result=(
                    ProvideCallbackResultInputResult(additional_properties=result)
                    if result is not None
                    else None
                ),
                error=error,
            ),
            id=self._operation_id("provide-callback-result"),
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        return asdict(output)

    async def close(self, session_id: str) -> dict[str, Any]:
        await self._a2a.execute_operation(
            A2AService.cancel_task,
            CancelTaskRequest(id=session_id),
            id=self._operation_id("cancel-task"),
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        return {"ok": True}


def _frames(items: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    frames: list[tuple[str, dict[str, Any]]] = []
    for item in items:
        event_type, data = _decode_item(item)
        if event_type == "reply_delta" and frames and frames[-1][0] == event_type:
            previous = frames[-1][1]
            if previous.get("turn_id") == data.get("turn_id"):
                previous["text"] = str(previous.get("text", "")) + str(
                    data.get("text", "")
                )
                previous["resume_offset"] = data["resume_offset"]
                continue
        frames.append((event_type, data))
    return frames


def _decode_item(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    response = StreamResponse()
    response.ParseFromString(base64.b64decode(str(item["data"])))
    body = response.WhichOneof("payload")
    if body is None:
        raise ValueError("A2A StreamResponse has no payload")
    value = getattr(response, body)
    metadata = MessageToDict(value.metadata, preserving_proto_field_name=True)
    encoded = metadata.get(HARNESS_EVENT_METADATA_KEY)
    resume_offset = int(item["offset"]) + 1
    if isinstance(encoded, str):
        payload = Payload()
        payload.ParseFromString(base64.b64decode(encoded))
        envelope = json.loads(payload.data)
        event = envelope["event"]
        return str(event["type"]), {
            **event,
            "agent_id": envelope["agent_id"],
            "turn_id": envelope["turn_id"],
            "turn_number": envelope["turn_number"],
            "timestamp": envelope["timestamp"],
            "resume_offset": resume_offset,
        }
    return "a2a_stream_response", {
        "response": MessageToDict(response, preserving_proto_field_name=True),
        "resume_offset": resume_offset,
    }


def _sse(event_type: str, data: dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()
