"""Microsoft Teams SDK adapter and Temporal activity worker."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from microsoft_teams.api import (
    Account,
    ApiClient,
    ConversationAccount,
    ConversationReference,
    MessageActivityInput,
)
from microsoft_teams.apps import App, HttpStream
from microsoft_teams.cards import AdaptiveCard
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from .contracts import (
    ApprovalPrompt,
    BeginStream,
    ContractError,
    FinishStream,
    TextMetadata,
    UpdateMessage,
    UpdateStream,
)

DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/teams/"
INITIAL_STREAMING_TEXT = "Thinking..."
MESSAGE_UPDATE_INTERVAL_SECONDS = 0.3
STREAM_START_TIMEOUT_SECONDS = 30.0
STREAM_MODE_NATIVE = "native"
STREAM_MODE_MESSAGE_UPDATE = "message-update"


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
            task_queue=os.getenv("CONNECTOR_TASK_QUEUE", "nexus-connector-teams").strip() or "nexus-connector-teams",
        )


def approval_card(prompt: ApprovalPrompt) -> AdaptiveCard:
    body: list[dict[str, object]] = [
        {
            "type": "TextBlock",
            "text": "🔐 Tool approval required",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "FactSet",
            "facts": [{"title": "Tool", "value": prompt.tool_name}],
        },
    ]
    if prompt.tool_input:
        body.append(
            {
                "type": "TextBlock",
                "text": prompt.tool_input,
                "wrap": True,
                "fontType": "Monospace",
                "isSubtle": True,
            }
        )

    decision = {
        "s": prompt.metadata.session_id,
        "t": prompt.tool_id,
        "n": prompt.tool_name,
    }
    return AdaptiveCard.model_validate(
        {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "body": body,
            "actions": [
                {"type": "Action.Submit", "title": "✅ Approve", "data": {**decision, "a": True}},
                {"type": "Action.Submit", "title": "❌ Deny", "data": {**decision, "a": False}},
            ],
        }
    )


def _sent_id(sent: object) -> str:
    value = getattr(sent, "id", "")
    if not isinstance(value, str) or not value or value == "DO_NOT_USE_PLACEHOLDER_ID":
        raise ValueError("Teams activity response missing id")
    return value


def _streaming_allowed(conversation_type: str) -> bool:
    """Teams SDK native streaming is supported only in 1:1 conversations."""
    return conversation_type.strip().lower() not in {"channel", "groupchat"}


@dataclass(slots=True)
class ActiveStream:
    """Track the transport mode and optional native Teams stream."""

    transport_mode: str
    native_stream: Any | None = None


class TeamsPlatform:
    def __init__(
        self,
        *,
        app_id: str,
        default_service_url: str,
        api_factory: Callable[[str], Any],
        worker_task_queue: str,
        stream_factory: Callable[[Any, ConversationReference], Any] = HttpStream,
        app: App | None = None,
    ) -> None:
        self.app_id = app_id
        self.default_service_url = default_service_url
        self.api_factory = api_factory
        self.worker_task_queue = worker_task_queue
        self.stream_factory = stream_factory
        self.app = app
        self.streams: dict[str, ActiveStream] = {}

    @classmethod
    def from_settings(cls, settings: Settings, worker_task_queue: str) -> TeamsPlatform:
        app = App(
            client_id=settings.microsoft_app_id,
            client_secret=settings.microsoft_app_password,
            tenant_id=settings.microsoft_tenant_id,
            service_url=settings.teams_service_url,
        )

        def api_factory(service_url: str) -> ApiClient:
            return ApiClient(service_url, app.api.http, cloud=app.cloud)

        return cls(
            app_id=settings.microsoft_app_id,
            default_service_url=settings.teams_service_url,
            api_factory=api_factory,
            worker_task_queue=worker_task_queue,
            app=app,
        )

    def _api(self, metadata: TextMetadata) -> Any:
        return self.api_factory(metadata.service_url.strip() or self.default_service_url)

    def _activities(self, metadata: TextMetadata) -> Any:
        """Return the conversation-bound activity operations in Teams SDK 2.0.x."""
        return self._api(metadata).conversations.activities(metadata.conversation_id)

    def _base_activity(self, metadata: TextMetadata, activity: MessageActivityInput) -> MessageActivityInput:
        activity.with_service_url(metadata.service_url.strip() or self.default_service_url)
        activity.with_channel_id(metadata.channel_id or "msteams")
        activity.with_from(Account(id=self.app_id))
        activity.with_conversation(ConversationAccount(id=metadata.conversation_id))
        return activity

    def _conversation_reference(self, metadata: TextMetadata) -> ConversationReference:
        return ConversationReference(
            activity_id=metadata.thread_id or None,
            user=Account(id=metadata.sender_id) if metadata.sender_id else None,
            bot=Account(id=self.app_id),
            conversation=ConversationAccount(id=metadata.conversation_id),
            channel_id=metadata.channel_id or "msteams",
            service_url=metadata.service_url.strip() or self.default_service_url,
        )

    async def _create_or_reply(self, metadata: TextMetadata, activity: MessageActivityInput) -> str:
        activities = self._activities(metadata)
        if metadata.thread_id:
            sent = await activities.reply(metadata.thread_id, activity)
        else:
            sent = await activities.create(activity)
        return _sent_id(sent)

    async def begin_stream(self, request: BeginStream) -> dict[str, object]:
        metadata = request.metadata
        # Teams SDK streaming is 1:1-only; shared conversations use message updates.
        if not _streaming_allowed(request.conversation_type):
            mode = STREAM_MODE_MESSAGE_UPDATE
            stream_id = await self._begin_message_updates(metadata)
        else:
            mode = STREAM_MODE_NATIVE
            try:
                stream_id = await self._begin_native_stream(metadata)
            except Exception as error:
                logging.warning("Teams native stream could not start; using message updates: %s", error)
                mode = STREAM_MODE_MESSAGE_UPDATE
                stream_id = await self._begin_message_updates(metadata)

        return {
            "ID": stream_id,
            "SessionID": metadata.session_id,
            "TransportMode": mode,
            "TaskQueue": self.worker_task_queue,
            "CloseBeforeApproval": True,
        }

    async def _begin_native_stream(self, metadata: TextMetadata) -> str:
        """Start the Teams SDK stream used for supported 1:1 conversations."""
        text = metadata.text if metadata.text.strip() else INITIAL_STREAMING_TEXT
        stream = self.stream_factory(self._api(metadata), self._conversation_reference(metadata))
        first_chunk: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        async def capture_first_chunk(sent: object) -> None:
            if not first_chunk.done():
                first_chunk.set_result(sent)

        stream.on_chunk(capture_first_chunk)
        stream.update(text)
        sent = await asyncio.wait_for(first_chunk, timeout=STREAM_START_TIMEOUT_SECONDS)
        stream_id = _sent_id(sent)
        self.streams[stream_id] = ActiveStream(transport_mode=STREAM_MODE_NATIVE, native_stream=stream)
        return stream_id

    async def _begin_message_updates(self, metadata: TextMetadata) -> str:
        """Start message replacements for channels and group chats."""
        text = metadata.text if metadata.text.strip() else INITIAL_STREAMING_TEXT
        activity = self._base_activity(metadata, MessageActivityInput(text=text).with_text_format("markdown"))
        stream_id = await self._create_or_reply(metadata, activity)
        self.streams[stream_id] = ActiveStream(transport_mode=STREAM_MODE_MESSAGE_UPDATE)
        return stream_id

    async def update_stream(self, request: UpdateStream) -> None:
        if not request.delta:
            return
        stream = self.streams.get(request.handle.id)
        if stream is None:
            raise ValueError(f"Teams stream {request.handle.id!r} is not active on this worker")
        if stream.transport_mode != request.handle.transport_mode:
            raise ValueError("Teams stream transport mode does not match handle")
        if stream.native_stream is not None:
            stream.native_stream.emit(request.delta)
            return
        await self._update_message(request.metadata, request.handle.id, request.full_text)
        # Space message updates to avoid Teams rate limits.
        await asyncio.sleep(MESSAGE_UPDATE_INTERVAL_SECONDS)

    async def finish_stream(self, request: FinishStream) -> None:
        stream = self.streams.get(request.handle.id)
        if stream is None:
            raise ValueError(f"Teams stream {request.handle.id!r} is not active on this worker")
        if stream.transport_mode != request.handle.transport_mode:
            raise ValueError("Teams stream transport mode does not match handle")
        try:
            if stream.native_stream is None:
                await self._update_message(request.metadata, request.handle.id, request.full_text)
                return
            if stream.native_stream.canceled:
                return
            stream.native_stream.clear_text()
            stream.native_stream.emit(MessageActivityInput(text=request.full_text).with_text_format("markdown"))
            result = await stream.native_stream.close()
            if result is None and not stream.native_stream.canceled:
                raise RuntimeError("Teams SDK stream did not produce a final activity")
        finally:
            self.streams.pop(request.handle.id, None)

    async def post_message(self, metadata: TextMetadata) -> None:
        if not metadata.text.strip():
            raise ValueError("text is required")
        activity = self._base_activity(metadata, MessageActivityInput(text=metadata.text).with_text_format("markdown"))
        await self._create_or_reply(metadata, activity)

    async def post_approval_prompt(self, prompt: ApprovalPrompt) -> None:
        activity = self._base_activity(
            prompt.metadata,
            MessageActivityInput(text="Tool approval required").add_card(approval_card(prompt)),
        )
        await self._create_or_reply(prompt.metadata, activity)

    async def update_message(self, request: UpdateMessage) -> None:
        await self._update_message(request.metadata, request.message_id, request.metadata.text)

    async def _update_message(self, metadata: TextMetadata, activity_id: str, text: str) -> None:
        activity = self._base_activity(metadata, MessageActivityInput(text=text).with_text_format("markdown"))
        await self._activities(metadata).update(activity_id, activity)


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


def _worker_task_queue(shared_task_queue: str) -> str:
    return f"{shared_task_queue}-stream-{uuid4().hex}"


async def run() -> None:
    settings = Settings.from_env()
    worker_task_queue = _worker_task_queue(settings.task_queue)
    platform = TeamsPlatform.from_settings(settings, worker_task_queue)
    if platform.app is not None:
        await platform.app.initialize()

    temporal = await Client.connect(settings.temporal_address, namespace=settings.connector_namespace)
    activities = TeamsActivities(platform)
    shared_worker = Worker(
        temporal,
        task_queue=settings.task_queue,
        activities=[
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
