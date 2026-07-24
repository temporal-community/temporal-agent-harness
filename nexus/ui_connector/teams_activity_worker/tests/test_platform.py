import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from teams_activity_worker.contracts import (
    ApprovalPrompt,
    BeginStream,
    FinishStream,
    StreamHandle,
    TextMetadata,
    UpdateMessage,
    UpdateStream,
)
from teams_activity_worker.platform import (
    STREAM_MODE_NATIVE,
    TeamsPlatform,
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


class FakeStream:
    def __init__(self) -> None:
        self.canceled = False
        self.calls: list[tuple[Any, ...]] = []
        self._chunk_handler = None

    def on_chunk(self, handler) -> None:
        self._chunk_handler = handler

    def update(self, text: str) -> None:
        self.calls.append(("update", text))
        assert self._chunk_handler is not None
        asyncio.create_task(self._chunk_handler(Sent("stream-1")))

    def emit(self, activity: object) -> None:
        self.calls.append(("emit", activity))

    async def close(self) -> Sent:
        self.calls.append(("close",))
        return Sent("stream-1")


@pytest.fixture
def fixture() -> tuple[TeamsPlatform, FakeConversations, FakeStream]:
    conversations = FakeConversations()
    stream = FakeStream()
    platform = TeamsPlatform(
        app_id="bot-1",
        default_service_url="https://default.test/teams/",
        api_factory=lambda _service_url: FakeApi(conversations),
        worker_task_queue="teams-worker-1",
        stream_factory=lambda _api, _ref: stream,
    )
    return platform, conversations, stream


def metadata(*, text: str = "", thread_id: str = "", service_url: str = "https://tenant.test/teams/"):
    return TextMetadata(
        sender_id="user-1",
        session_id="teams:conversation-1",
        thread_id=thread_id,
        text=text,
        service_url=service_url,
        channel_id="msteams",
    )


@pytest.mark.asyncio
async def test_begin_personal_chat_uses_native_streaming(fixture) -> None:
    platform, conversations, stream = fixture

    handle = await platform.begin_stream(BeginStream(metadata=metadata(), conversation_type="personal"))

    assert handle == {
        "ID": "stream-1",
        "SessionID": "teams:conversation-1",
        "TransportMode": STREAM_MODE_NATIVE,
        "TaskQueue": "teams-worker-1",
        "CloseBeforeApproval": True,
    }
    assert stream.calls == [("update", "Thinking...")]
    assert not conversations.calls
    assert "stream-1" in platform.streams


@pytest.mark.asyncio
async def test_begin_personal_chat_propagates_native_stream_failure() -> None:
    conversations = FakeConversations()

    def failed_stream(_api, _reference):
        raise RuntimeError("native stream failed")

    platform = TeamsPlatform(
        app_id="bot-1",
        default_service_url="https://default.test/teams/",
        api_factory=lambda _service_url: FakeApi(conversations),
        worker_task_queue="teams-worker-1",
        stream_factory=failed_stream,
    )

    with pytest.raises(RuntimeError, match="native stream failed"):
        await platform.begin_stream(BeginStream(metadata=metadata(), conversation_type="personal"))

    assert not conversations.calls
    assert not platform.streams


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation_type", ["channel", "groupChat"])
async def test_begin_non_personal_chat_rejects_native_streaming(fixture, conversation_type: str) -> None:
    platform, conversations, _ = fixture

    with pytest.raises(ValueError, match="only in personal conversations"):
        await platform.begin_stream(
            BeginStream(metadata=metadata(text="first", thread_id="root-1"), conversation_type=conversation_type)
        )
    assert not conversations.calls


@pytest.mark.asyncio
async def test_native_update_posts_stream_entity(fixture) -> None:
    platform, _, stream = fixture
    await platform.begin_stream(BeginStream(metadata=metadata(), conversation_type="personal"))
    handle = StreamHandle("stream-1", "teams:conversation-1", STREAM_MODE_NATIVE, "teams-worker-1")

    await platform.update_stream(UpdateStream(metadata(), handle, "hello"))

    assert stream.calls[-1][0] == "emit"
    emitted = stream.calls[-1][1]
    assert emitted.text == "hello"
    assert emitted.text_format == "markdown"


@pytest.mark.asyncio
async def test_native_finish_posts_final_stream_message(fixture) -> None:
    platform, _, stream = fixture
    await platform.begin_stream(BeginStream(metadata=metadata(), conversation_type="personal"))
    handle = StreamHandle("stream-1", "teams:conversation-1", STREAM_MODE_NATIVE, "teams-worker-1")

    await platform.update_stream(UpdateStream(metadata(), handle, "complete"))
    await platform.finish_stream(FinishStream(metadata(), handle))

    assert stream.calls[-2][0] == "emit"
    assert stream.calls[-2][1].text == "complete"
    assert stream.calls[-1] == ("close",)
    assert "stream-1" not in platform.streams


@pytest.mark.asyncio
async def test_approval_prompt_uses_sdk_adaptive_card(fixture) -> None:
    platform, conversations, _ = fixture
    prompt = ApprovalPrompt(metadata(), "tool-1", "deploy", "{}")

    await platform.post_approval_prompt(prompt)

    activity = conversations.calls[0][2]
    payload = activity.model_dump(by_alias=True, exclude_none=True)
    card = payload["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    assert card["actions"][0]["data"]["t"] == "tool-1"


@pytest.mark.asyncio
async def test_update_message_replaces_approval_card(fixture) -> None:
    platform, conversations, _ = fixture

    await platform.update_message(UpdateMessage(metadata(text="resolved"), "card-1"))

    assert conversations.calls[0][0:3] == ("update", "conversation-1", "card-1")
    payload = conversations.calls[0][3].model_dump(by_alias=True, exclude_none=True)
    assert payload["text"] == "resolved"


@pytest.mark.asyncio
async def test_update_rejects_stream_owned_by_another_worker(fixture) -> None:
    platform, _, _ = fixture
    handle = StreamHandle("missing", "teams:conversation-1", STREAM_MODE_NATIVE, "another-worker")

    with pytest.raises(ValueError, match="not active"):
        await platform.update_stream(UpdateStream(metadata(), handle, "hello"))
