"""Environment configuration and process lifecycle for the Teams activity worker."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from a2a.types import StreamResponse, TaskState
from google.protobuf.json_format import MessageToDict
from microsoft_teams.api import ApiClient
from microsoft_teams.apps import App
from temporalio import activity
from temporalio.api.common.v1 import Payload
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from .contracts import (
    ApprovalPrompt,
    BeginStream,
    ContractError,
    FinishStream,
    StreamHandle,
    TextMetadata,
    UpdateMessage,
    UpdateStream,
)
from .platform import TeamsPlatform

DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/teams/"


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    microsoft_tenant_id: str
    microsoft_app_id: str
    microsoft_app_password: str
    teams_service_url: str = DEFAULT_SERVICE_URL
    temporal_address: str = "localhost:7233"
    connector_namespace: str = "connector"
    task_queue: str = "nexus-connector-teams"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            microsoft_tenant_id=_required("MICROSOFT_TENANT_ID"),
            microsoft_app_id=_required("MICROSOFT_APP_ID"),
            microsoft_app_password=_required("MICROSOFT_APP_PASSWORD"),
            teams_service_url=os.getenv("TEAMS_SERVICE_URL", DEFAULT_SERVICE_URL).strip() or DEFAULT_SERVICE_URL,
            temporal_address=os.getenv("TEMPORAL_ADDRESS", "localhost:7233").strip() or "localhost:7233",
            connector_namespace=os.getenv("CONNECTOR_NAMESPACE", "connector").strip() or "connector",
            task_queue=os.getenv("TEAMS_DRIVER_TASK_QUEUE", "nexus-connector-teams").strip()
            or "nexus-connector-teams",
        )


def _parse(parser, payload: dict[str, Any]):
    try:
        return parser(payload)
    except (ContractError, TypeError, ValueError) as error:
        raise ApplicationError(str(error), type="InvalidTeamsActivityInput", non_retryable=True) from error


class TeamsActivities:
    def __init__(self, platform: TeamsPlatform) -> None:
        self.platform = platform

    @activity.defn(name="BeginStream")
    async def begin_stream(self, payload: dict[str, Any]) -> dict[str, object]:
        return await self.platform.begin_stream(_parse(BeginStream.from_payload, payload))

    @activity.defn(name="UpdateStream")
    async def update_stream(self, payload: dict[str, Any]) -> None:
        await self.platform.update_stream(_parse(UpdateStream.from_payload, payload))

    @activity.defn(name="FinishStream")
    async def finish_stream(self, payload: dict[str, Any]) -> None:
        await self.platform.finish_stream(_parse(FinishStream.from_payload, payload))

    @activity.defn(name="PostMessage")
    async def post_message(self, payload: dict[str, Any]) -> None:
        await self.platform.post_message(_parse(TextMetadata.from_payload, payload))

    @activity.defn(name="PostApprovalPrompt")
    async def post_approval_prompt(self, payload: dict[str, Any]) -> None:
        await self.platform.post_approval_prompt(_parse(ApprovalPrompt.from_payload, payload))

    @activity.defn(name="UpdateActivity")
    async def update_message(self, payload: dict[str, Any]) -> None:
        await self.platform.update_message(_parse(UpdateMessage.from_payload, payload))

    @activity.defn(name="TeamsAcknowledgeApproval")
    async def acknowledge_approval(self, payload: dict[str, Any]) -> dict[str, Any]:
        context = payload.get("context") or {}
        metadata = TextMetadata.from_payload(context.get("metadata") or {})
        approved = bool(context.get("approved"))
        tool_name = str(context.get("toolName") or "tool")
        decision = "✅ Approved" if approved else "❌ Denied"
        await self.platform.update_message(
            UpdateMessage(
                metadata=TextMetadata(
                    sender_id=metadata.sender_id,
                    session_id=metadata.session_id,
                    thread_id=metadata.thread_id,
                    text=f"🔐 Tool `{tool_name}`: {decision}",
                    service_url=metadata.service_url,
                    channel_id=metadata.channel_id,
                ),
                message_id=str(context.get("activityId") or metadata.thread_id),
            )
        )
        return {}

    @activity.defn(name="TeamsDeliverA2A")
    async def deliver_a2a(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Render a lossless A2A page at the Teams edge.

        The Go tunnel treats both ``context`` and ``state`` as opaque JSON. This
        activity alone chooses how Teams represents harness extension events.
        """
        context = payload.get("context") or {}
        metadata = TextMetadata.from_payload(context.get("metadata") or {})
        conversation_type = str(context.get("conversationType") or "")
        state = dict(payload.get("state") or {})
        handle_payload = state.get("handle")
        handle = StreamHandle.from_payload(handle_payload) if handle_payload else None
        accumulated_text = str(state.get("text") or "")
        supports_streaming = conversation_type.strip().lower() not in {
            "channel",
            "groupchat",
        }
        pending_text: list[str] = []
        turn_complete = False

        async def flush() -> None:
            nonlocal accumulated_text, handle
            if not pending_text:
                return
            text = "".join(pending_text)
            pending_text.clear()
            if not supports_streaming:
                accumulated_text += text
                return
            if handle is None:
                handle = StreamHandle.from_payload(
                    await self.platform.begin_stream(
                        BeginStream(metadata=metadata, conversation_type=conversation_type)
                    )
                )
            await self.platform.update_stream(UpdateStream(metadata=metadata, handle=handle, delta=text))

        for item in payload.get("items") or []:
            event = _decode_harness_event(str(item.get("data") or ""))
            if event is None:
                continue
            event_type = event.get("type")
            if event_type == "reply_delta":
                pending_text.append(str(event.get("text") or ""))
            elif event_type == "thought_summary":
                delta = event.get("delta") or {}
                pending_text.append(str(delta.get("text") or ""))
            elif event_type == "tool_start":
                pending_text.append(f"\n_{event.get('tool_name', 'tool')}..._")
            elif event_type == "tool_end":
                pending_text.append(" ✅\n\n")
            elif event_type == "tool_error":
                pending_text.append(f" ❌ Error: {event.get('message', '')}\n\n")
            elif event_type == "error":
                pending_text.append(f"[error] {event.get('message', '')}")
                turn_complete = True
            elif event_type == "tool_approval_requested":
                await flush()
                if handle is not None:
                    await self.platform.finish_stream(
                        FinishStream(metadata=metadata, handle=handle)
                    )
                    handle = None
                await self.platform.post_approval_prompt(
                    ApprovalPrompt(
                        metadata=metadata,
                        tool_id=str(event.get("tool_id") or ""),
                        tool_name=str(event.get("tool_name") or ""),
                        tool_input=json.dumps(event.get("tool_input") or {}),
                    )
                )
            elif event_type == "reply":
                turn_complete = True

        await flush()
        if (turn_complete or bool(payload.get("closed"))) and handle is not None:
            await self.platform.finish_stream(FinishStream(metadata=metadata, handle=handle))
            handle = None

        if (turn_complete or bool(payload.get("closed"))) and not supports_streaming:
            if accumulated_text.strip():
                await self.platform.post_message(
                    TextMetadata(
                        sender_id=metadata.sender_id,
                        session_id=metadata.session_id,
                        thread_id=metadata.thread_id,
                        text=accumulated_text,
                        service_url=metadata.service_url,
                        channel_id=metadata.channel_id,
                    )
                )
            accumulated_text = ""

        state["handle"] = _handle_payload(handle) if handle is not None else None
        state["text"] = accumulated_text
        return {
            "state": state,
            "turnComplete": turn_complete,
            "taskQueue": handle.task_queue if handle is not None else "",
        }


def _decode_harness_event(encoded: str) -> dict[str, Any] | None:
    response = StreamResponse()
    response.ParseFromString(base64.b64decode(encoded))
    body = response.WhichOneof("payload")
    if body is None:
        return None
    metadata = MessageToDict(getattr(response, body).metadata, preserving_proto_field_name=True)
    extension = metadata.get("temporal.io/agent-event-payload")
    if not isinstance(extension, str):
        if body == "artifact_update":
            return {
                "type": "reply_delta",
                "text": _parts_text(response.artifact_update.artifact.parts),
            }
        if body == "message":
            return {"type": "reply_delta", "text": _parts_text(response.message.parts)}
        if body == "status_update":
            status = response.status_update.status
            if status.state in {
                TaskState.TASK_STATE_FAILED,
                TaskState.TASK_STATE_CANCELED,
                TaskState.TASK_STATE_REJECTED,
            }:
                return {
                    "type": "error",
                    "message": _parts_text(status.message.parts)
                    if status.HasField("message")
                    else "A2A task failed",
                }
            if status.state == TaskState.TASK_STATE_COMPLETED:
                return {"type": "reply"}
        return None
    temporal_payload = Payload()
    temporal_payload.ParseFromString(base64.b64decode(extension))
    envelope = json.loads(temporal_payload.data)
    return envelope.get("event")


def _parts_text(parts: Any) -> str:
    return "".join(part.text for part in parts if part.HasField("text"))


def _handle_payload(handle: StreamHandle) -> dict[str, str]:
    return {
        "ID": handle.id,
        "SessionID": handle.session_id,
        "TransportMode": handle.transport_mode,
        "TaskQueue": handle.task_queue,
    }


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    worker_task_queue = f"{settings.task_queue}-stream-{uuid4().hex}"
    app = App(
        client_id=settings.microsoft_app_id,
        client_secret=settings.microsoft_app_password,
        tenant_id=settings.microsoft_tenant_id,
        service_url=settings.teams_service_url,
    )

    def api_factory(service_url: str) -> ApiClient:
        return ApiClient(service_url, app.api.http, cloud=app.cloud)

    platform = TeamsPlatform(
        app_id=settings.microsoft_app_id,
        default_service_url=settings.teams_service_url,
        api_factory=api_factory,
        worker_task_queue=worker_task_queue,
        app=app,
    )
    if platform.app is not None:
        await platform.app.initialize()

    temporal = await Client.connect(settings.temporal_address, namespace=settings.connector_namespace)
    activities = TeamsActivities(platform)
    shared_worker = Worker(
        temporal,
        task_queue=settings.task_queue,
        activities=[
            activities.deliver_a2a,
            activities.acknowledge_approval,
            activities.begin_stream,
            activities.post_message,
            activities.post_approval_prompt,
            activities.update_message,
        ],
    )
    stream_worker = Worker(
        temporal,
        task_queue=worker_task_queue,
        activities=[
            activities.deliver_a2a,
            activities.update_stream,
            activities.finish_stream,
        ],
    )
    logging.info(
        "Starting Teams activity worker on shared queue %r and private stream queue %r",
        settings.task_queue,
        worker_task_queue,
    )
    try:
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(shared_worker.run())
            task_group.create_task(stream_worker.run())
    finally:
        if platform.app is not None:
            await platform.app.stop()


if __name__ == "__main__":
    asyncio.run(main())
