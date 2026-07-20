"""Microsoft Teams SDK adapter used by Temporal activities."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from microsoft_teams.api import (
    Account,
    ApiClient,
    ChannelData,
    ConversationAccount,
    MessageActivityInput,
    TypingActivityInput,
)
from microsoft_teams.apps import App

from .cards import approval_card
from .config import Settings
from .contracts import ApprovalPrompt, BeginStream, FinishStream, TextMetadata, UpdateActivity, UpdateStream

INITIAL_STREAMING_TEXT = "Thinking..."
INITIAL_STREAM_SEQUENCE = 1
MIN_STREAM_UPDATE_NANOSECONDS = 1_500_000_000
STREAM_MODE_NATIVE = "native"
STREAM_MODE_MESSAGE_UPDATE = "message-update"


ActivityInputT = TypeVar("ActivityInputT", MessageActivityInput, TypingActivityInput)


def _sent_id(sent: object) -> str:
    value = getattr(sent, "id", "")
    if not isinstance(value, str) or not value or value == "DO_NOT_USE_PLACEHOLDER_ID":
        raise ValueError("Teams activity response missing id")
    return value


def _streaming_allowed(conversation_type: str) -> bool:
    return conversation_type.strip().lower() not in {"channel", "groupchat"}


def _non_personal_streaming_error(error: Exception) -> bool:
    if not isinstance(error, httpx.HTTPStatusError) or error.response.status_code != 405:
        return False
    body = error.response.text.lower()
    return "streaming" in body and "non personal" in body


class TeamsPlatform:
    def __init__(
        self,
        *,
        app_id: str,
        default_service_url: str,
        api_factory: Callable[[str], Any],
        app: App | None = None,
    ) -> None:
        self.app_id = app_id
        self.default_service_url = default_service_url
        self.api_factory = api_factory
        self.app = app

    @classmethod
    def from_settings(cls, settings: Settings) -> TeamsPlatform:
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
            app=app,
        )

    def _api(self, metadata: TextMetadata) -> Any:
        return self.api_factory(metadata.service_url.strip() or self.default_service_url)

    def _activities(self, metadata: TextMetadata) -> Any:
        """Return the conversation-bound activity operations in Teams SDK 2.0.x."""
        return self._api(metadata).conversations.activities(metadata.conversation_id)

    def _base_activity(self, metadata: TextMetadata, activity: ActivityInputT) -> ActivityInputT:
        activity.with_service_url(metadata.service_url.strip() or self.default_service_url)
        activity.with_channel_id(metadata.channel_id or "msteams")
        activity.with_from(Account(id=self.app_id))
        activity.with_conversation(ConversationAccount(id=metadata.conversation_id))
        return activity

    async def _create_or_reply(self, metadata: TextMetadata, activity: MessageActivityInput) -> str:
        activities = self._activities(metadata)
        if metadata.thread_id:
            sent = await activities.reply(metadata.thread_id, activity)
        else:
            sent = await activities.create(activity)
        return _sent_id(sent)

    async def begin_stream(self, request: BeginStream) -> dict[str, object]:
        metadata = request.metadata
        mode = STREAM_MODE_NATIVE
        if not _streaming_allowed(request.conversation_type):
            mode = STREAM_MODE_MESSAGE_UPDATE
            stream_id = await self._begin_message_updates(metadata)
        else:
            try:
                stream_id = await self._begin_native_stream(metadata)
            except Exception as error:
                if request.conversation_type.strip() or not _non_personal_streaming_error(error):
                    raise
                mode = STREAM_MODE_MESSAGE_UPDATE
                stream_id = await self._begin_message_updates(metadata)

        return {
            "ID": stream_id,
            "SessionID": metadata.session_id,
            "TransportMode": mode,
            "WireTextMode": "full_text",
            "MinUpdateInterval": MIN_STREAM_UPDATE_NANOSECONDS,
            "CloseBeforeApproval": True,
            "NextSequence": 2 if mode == STREAM_MODE_NATIVE else 0,
        }

    async def _begin_native_stream(self, metadata: TextMetadata) -> str:
        text = metadata.text if metadata.text.strip() else INITIAL_STREAMING_TEXT
        activity = self._base_activity(
            metadata,
            TypingActivityInput(text=text).with_channel_data(ChannelData(stream_type="informative")),
        ).add_stream_update(INITIAL_STREAM_SEQUENCE)
        sent = await self._activities(metadata).create(activity)
        return _sent_id(sent)

    async def _begin_message_updates(self, metadata: TextMetadata) -> str:
        text = metadata.text if metadata.text.strip() else INITIAL_STREAMING_TEXT
        activity = self._base_activity(metadata, MessageActivityInput(text=text).with_text_format("markdown"))
        return await self._create_or_reply(metadata, activity)

    async def update_stream(self, request: UpdateStream) -> None:
        if not request.full_text:
            return
        if request.handle.transport_mode == STREAM_MODE_MESSAGE_UPDATE:
            await self._update_message(request.metadata, request.handle.id, request.full_text)
            return
        if request.handle.transport_mode != STREAM_MODE_NATIVE:
            raise ValueError(f"unknown Teams stream transport mode {request.handle.transport_mode!r}")
        if request.sequence <= INITIAL_STREAM_SEQUENCE:
            raise ValueError(f"Teams stream update sequence must be greater than {INITIAL_STREAM_SEQUENCE}")

        activity = self._base_activity(
            request.metadata,
            TypingActivityInput(text=request.full_text).with_id(request.handle.id),
        ).add_stream_update(request.sequence)
        await self._activities(request.metadata).create(activity)

    async def finish_stream(self, request: FinishStream) -> None:
        if request.handle.transport_mode == STREAM_MODE_MESSAGE_UPDATE:
            await self._update_message(request.metadata, request.handle.id, request.full_text)
            return
        if request.handle.transport_mode != STREAM_MODE_NATIVE:
            raise ValueError(f"unknown Teams stream transport mode {request.handle.transport_mode!r}")

        activity = self._base_activity(
            request.metadata,
            MessageActivityInput(text=request.full_text).with_text_format("markdown").with_id(request.handle.id),
        ).add_stream_final()
        await self._activities(request.metadata).create(activity)

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

    async def update_activity(self, request: UpdateActivity) -> None:
        await self._update_message(request.metadata, request.activity_id, request.metadata.text)

    async def _update_message(self, metadata: TextMetadata, activity_id: str, text: str) -> None:
        activity = self._base_activity(metadata, MessageActivityInput(text=text).with_text_format("markdown"))
        await self._activities(metadata).update(activity_id, activity)
