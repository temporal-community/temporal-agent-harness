# ABOUTME: Unit tests for LangGraphStreamObserver, fed through the vendored plugin's bridge from
# ABOUTME: LangChain callbacks: which model calls stream, the events they become, and refusals.

# pyright: reportMissingTypeStubs=false

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain_core.callbacks import AsyncCallbackManager, BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.chat_models import generate_from_stream
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tools import tool

from temporalio.exceptions import ApplicationError

from temporal_agent_harness.ai_sdks.langgraph._activity import _observing
from temporal_agent_harness.ai_sdks.langgraph._harness import (
    LangGraphStreamObserver,
    _non_retryable,
    _token_usage,
)
from temporal_agent_harness.harness.agent_protocol import ReplyDelta, TokenUsage, ToolRequested

pytestmark = pytest.mark.asyncio(loop_scope="module")


def _reply(messages: list[BaseMessage]) -> list[AIMessageChunk]:
    """"ask" -> one ask_user call; anything else -> "You said: <text>"."""
    text = messages[-1].text
    if "ask" in text:
        call = {"name": "ask_user", "args": '{"question": "Which city?"}', "id": "call-ask", "index": 0}
        return [AIMessageChunk(content="", tool_call_chunks=[call])]
    return [AIMessageChunk(content=f"You said: {text}")]


class FakeChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fake"

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        return generate_from_stream(ChatGenerationChunk(message=c) for c in _reply(messages))

    async def _astream(  # type: ignore[override]
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in _reply(messages):
            yield ChatGenerationChunk(message=chunk)


class Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)


def recording_observer() -> tuple[LangGraphStreamObserver, Recorder]:
    """An observer that publishes to a recorder instead of the turn stream."""
    observer = LangGraphStreamObserver(cast(Any, None))
    recorder = Recorder()
    observer._publisher = cast(Any, recorder)
    return observer, recorder


async def observed(run: Callable[[], Awaitable[Any]]) -> list[Any]:
    """What the observer publishes for the model calls ``run`` makes in an observed activity."""
    observer, recorder = recording_observer()
    async with _observing(observer):
        await run()
    return recorder.events


def replies(events: list[Any]) -> list[str]:
    return [e.text for e in events if isinstance(e, ReplyDelta)]


async def test_streams_model_calls_in_chains_but_not_under_tools_or_nostream():
    chain = RunnableLambda(lambda x: x) | FakeChatModel()

    @tool
    async def consult(question: str) -> str:
        """A tool that asks a model, whose text is not the reply."""
        return (await chain.ainvoke(question)).text

    assert replies(await observed(lambda: FakeChatModel().ainvoke("hello"))) == ["You said: hello"]
    assert replies(await observed(lambda: chain.ainvoke("hello"))) == ["You said: hello"]
    assert await observed(lambda: chain.with_config(tags=["nostream"]).ainvoke("hello")) == []
    assert await observed(lambda: consult.ainvoke({"question": "hello"})) == []


async def test_a_model_call_under_an_unseen_parent_streams():
    # A parent the observer never saw, as under a LangSmith trace.
    observer, recorder = recording_observer()
    run_id, parent = str(uuid4()), str(uuid4())
    base = {"run_id": run_id, "parent_run_id": parent, "tags": [], "metadata": {}}
    await observer.on_event({**base, "event": "on_chat_model_start", "data": {}})
    await observer.on_event({**base, "event": "on_chat_model_stream", "data": {"chunk": AIMessageChunk(content="hi")}})
    await observer.on_event({**base, "event": "on_chat_model_end", "data": {"output": AIMessage(content="hi")}})
    assert [type(e).__name__ for e in recorder.events] == [
        "ModelInteractionStarted",
        "ReplyDelta",
        "ModelInteractionEnded",
    ]


async def test_the_bridge_keeps_the_run_s_existing_callbacks():
    existing = BaseCallbackHandler()
    observer, _ = recording_observer()
    for callbacks in ([existing], AsyncCallbackManager([existing])):
        token = var_child_runnable_config.set({"callbacks": callbacks})
        try:
            async with _observing(observer):
                seen = var_child_runnable_config.get()["callbacks"]
                handlers = seen if isinstance(seen, list) else seen.handlers
                assert handlers[0] is existing and len(handlers) == 2
        finally:
            var_child_runnable_config.reset(token)


async def test_a_sync_model_call_on_a_thread_is_observed_in_order():
    events = await observed(lambda: asyncio.to_thread(FakeChatModel().invoke, "hello"))
    assert [type(e).__name__ for e in events] == [
        "ModelInteractionStarted",
        "ReplyDelta",
        "ModelInteractionEnded",
    ]


async def test_tool_calls_are_requested_before_the_interaction_ends():
    events = await observed(lambda: FakeChatModel().ainvoke("ask"))
    assert [type(e).__name__ for e in events] == [
        "ModelInteractionStarted",
        "ToolRequested",
        "ModelInteractionEnded",
    ]
    assert events[1] == ToolRequested(
        tool_id="call-ask", tool_name="ask_user", tool_input={"question": "Which city?"}
    )


async def test_non_streaming_model_publishes_its_reply():
    events = await observed(lambda: FakeChatModel(disable_streaming=True).ainvoke("hello"))
    assert [type(e).__name__ for e in events] == [
        "ModelInteractionStarted",
        "ReplyDelta",
        "ModelInteractionEnded",
    ]
    assert events[1].text == "You said: hello"


def test_usage_without_details_maps_to_empty_usage():
    no_details = cast(Any, {"input_token_details": None, "output_token_details": None})
    assert _token_usage(no_details) == TokenUsage()


def test_only_caller_errors_are_non_retryable():
    def error(status: object) -> Exception:
        return type("ProviderError", (Exception,), {"status_code": status})()

    statuses = [400, 401, 404, 408, 409, 429, 500, 503, None, "401"]
    assert [s for s in statuses if _non_retryable(error(s))] == [400, 401, 404]


async def test_a_caller_error_fails_the_activity_without_retrying():
    observer = LangGraphStreamObserver(cast(Any, None))
    bad_key = type("AuthError", (Exception,), {"status_code": 401})("bad key")
    with pytest.raises(ApplicationError) as failed:
        await observer.__aexit__(type(bad_key), bad_key, None)
    assert failed.value.non_retryable
    overloaded = type("Overloaded", (Exception,), {"status_code": 529})("try later")
    assert await observer.__aexit__(type(overloaded), overloaded, None) is False


def test_citations_and_hosted_tools_reach_the_stream():
    observer, recorder = recording_observer()
    cite = {"type": "citation", "url": "https://x.test", "start_index": 0, "end_index": 2}
    observer._publish_content(
        cast(Any, recorder),
        AIMessage(
            content=[
                {"type": "server_tool_call", "id": "ws_1", "name": "web_search", "args": {"query": "q"}},
                {"type": "server_tool_result", "tool_call_id": "ws_1", "status": "success"},
                {"type": "server_tool_result", "tool_call_id": "ws_1", "status": "success"},
                {"type": "text", "text": "hi", "annotations": [cite]},
            ]
        ),
    )

    assert [type(e).__name__ for e in recorder.events] == [
        "ToolRequested",
        "ToolStartEvent",
        "ToolEndEvent",
        "ReplyDelta",
        "TextAnnotationDelta",
    ]
    assert recorder.events[0].tool_id == "ws_1" and recorder.events[0].tool_input == {"query": "q"}
    assert recorder.events[2].tool_name == "web_search"
    assert recorder.events[4].delta == cite
