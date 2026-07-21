from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

import pytest

from teams_activity_worker.contracts import (
    ApprovalPrompt,
    BeginStream,
    FinishStream,
    StreamHandle,
    TextMetadata,
    UpdateActivity,
    UpdateStream,
)
from teams_activity_worker.platform import (
    MIN_STREAM_UPDATE_NANOSECONDS,
    STREAM_MODE_MESSAGE_UPDATE,
    STREAM_MODE_NATIVE,
    Settings,
    TeamsPlatform,
    main,
)


@dataclass
class Sent:
    id: str


class FakeActivityOperations:
    def __init__(self, calls: list[tuple[Any, ...]], conversation_id: str) -> None:
        self.calls = calls
        self.conversation_id = conversation_id

    async def create(self, activity: object) -> Sent:
        self.calls.append(("create", self.conversation_id, activity))
        return Sent("activity-1")

    async def reply(self, activity_id: str, activity: object) -> Sent:
        self.calls.append(("reply", self.conversation_id, activity_id, activity))
        return Sent("reply-1")

    async def update(self, activity_id: str, activity: object) -> Sent:
        self.calls.append(("update", self.conversation_id, activity_id, activity))
        return Sent(activity_id)


class FakeConversations:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def activities(self, conversation_id: str) -> FakeActivityOperations:
        return FakeActivityOperations(self.calls, conversation_id)


@dataclass
class FakeApi:
    conversations: FakeConversations


@pytest.fixture
def fixture() -> tuple[TeamsPlatform, FakeConversations]:
    conversations = FakeConversations()
    platform = TeamsPlatform(
        app_id="bot-1",
        default_service_url="https://default.test/teams/",
        api_factory=lambda _service_url: FakeApi(conversations),
    )
    return platform, conversations


def metadata(*, text: str = "", thread_id: str = "", service_url: str = "https://tenant.test/teams/"):
    return TextMetadata(
        sender_id="user-1",
        session_id="teams:conversation-1",
        thread_id=thread_id,
        text=text,
        service_url=service_url,
        channel_id="msteams",
    )


def test_console_script_runs_platform_main() -> None:
    entry_point = next(item for item in entry_points(group="console_scripts") if item.name == "teams-activity-worker")

    assert entry_point.value == "teams_activity_worker.platform:main"
    assert entry_point.load() is main


@pytest.mark.asyncio
async def test_begin_personal_chat_uses_native_streaming(fixture) -> None:
    platform, conversations = fixture

    handle = await platform.begin_stream(BeginStream(metadata=metadata(), conversation_type="personal"))

    assert handle == {
        "ID": "activity-1",
        "SessionID": "teams:conversation-1",
        "TransportMode": STREAM_MODE_NATIVE,
        "WireTextMode": "full_text",
        "MinUpdateInterval": MIN_STREAM_UPDATE_NANOSECONDS,
        "CloseBeforeApproval": True,
        "NextSequence": 2,
    }
    _, conversation_id, activity = conversations.calls[0]
    payload = activity.model_dump(by_alias=True, exclude_none=True)
    assert conversation_id == "conversation-1"
    assert payload["type"] == "typing"
    assert payload["text"] == "Thinking..."
    assert payload["channelData"]["streamType"] == "informative"
    assert payload["entities"][0]["streamSequence"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation_type", ["channel", "groupChat"])
async def test_begin_non_personal_chat_replies_with_updatable_message(fixture, conversation_type: str) -> None:
    platform, conversations = fixture

    handle = await platform.begin_stream(
        BeginStream(metadata=metadata(text="first", thread_id="root-1"), conversation_type=conversation_type)
    )

    assert handle["TransportMode"] == STREAM_MODE_MESSAGE_UPDATE
    assert handle["ID"] == "reply-1"
    assert conversations.calls[0][0:3] == ("reply", "conversation-1", "root-1")


@pytest.mark.asyncio
async def test_native_update_posts_stream_entity(fixture) -> None:
    platform, conversations = fixture
    handle = StreamHandle("stream-1", "teams:conversation-1", STREAM_MODE_NATIVE, 2)

    await platform.update_stream(UpdateStream(metadata(), handle, "hello", 2))

    operation, _, activity = conversations.calls[0]
    payload = activity.model_dump(by_alias=True, exclude_none=True)
    assert operation == "create"
    assert payload["id"] == "stream-1"
    assert payload["entities"][0]["streamType"] == "streaming"
    assert payload["entities"][0]["streamSequence"] == 2


@pytest.mark.asyncio
async def test_native_finish_posts_final_stream_message(fixture) -> None:
    platform, conversations = fixture
    handle = StreamHandle("stream-1", "teams:conversation-1", STREAM_MODE_NATIVE, 3)

    await platform.finish_stream(FinishStream(metadata(), handle, "complete"))

    operation, _, activity = conversations.calls[0]
    payload = activity.model_dump(by_alias=True, exclude_none=True)
    assert operation == "create"
    assert payload["type"] == "message"
    assert payload["entities"][0]["streamType"] == "final"


@pytest.mark.asyncio
async def test_approval_prompt_uses_sdk_adaptive_card(fixture) -> None:
    platform, conversations = fixture
    prompt = ApprovalPrompt(metadata(), "tool-1", "deploy", "{}")

    await platform.post_approval_prompt(prompt)

    activity = conversations.calls[0][2]
    payload = activity.model_dump(by_alias=True, exclude_none=True)
    card = payload["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["actions"][0]["data"]["t"] == "tool-1"


@pytest.mark.asyncio
async def test_update_activity_replaces_approval_card(fixture) -> None:
    platform, conversations = fixture

    await platform.update_activity(UpdateActivity(metadata(text="resolved"), "card-1"))

    assert conversations.calls[0][0:3] == ("update", "conversation-1", "card-1")
    payload = conversations.calls[0][3].model_dump(by_alias=True, exclude_none=True)
    assert payload["text"] == "resolved"


@pytest.mark.asyncio
async def test_sdk_app_initializes_for_proactive_messaging() -> None:
    platform = TeamsPlatform.from_settings(
        Settings(
            microsoft_tenant_id="tenant",
            microsoft_app_id="app",
            microsoft_app_password="secret",
        )
    )

    assert platform.app is not None
    await platform.app.initialize()
    activity_operations = platform._activities(metadata())
    assert callable(activity_operations.create)
    assert callable(activity_operations.reply)
    assert callable(activity_operations.update)
    await platform.app.stop()
