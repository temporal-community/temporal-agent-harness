# ABOUTME: Tests that the OpenAI Agents live translator (OpenAIStreamObserver) does not silently
# drop a function call's tool_requested. Covers the two ways it can: the Chat Completions backend
# (OpenAIChatCompletionsModel, LiteLLM, any third-party model) closes a function call with
# response.output_item.done and never emits …arguments.done, and a stream that ends or raises
# mid-call emits neither terminal. Drives the REAL agents ChatCmplStreamHandler with synthetic chat
# chunks so the event shapes are the SDK's own, not our guess at them, against an in-memory
# publisher (no Temporal server, no OPENAI_API_KEY). Complements test_stream_observer.py, which
# covers the Responses backend's happy path.
#
# Run with: uv run pytest tests/ai_sdks/openai_agents/test_stream_observer_tool_requested.py -v

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from openai.types.chat import ChatCompletionChunk
from openai.types.responses import (
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
)
from openai.types.responses.response import Response

from agents.models.chatcmpl_stream_handler import ChatCmplStreamHandler

from temporal_agent_harness.ai_sdks import openai_agents_harness as h
from temporal_agent_harness.harness.agent_protocol import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    ToolRequested,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
from temporal_agent_harness.harness.stream_context import TurnStreamContext


class _FakePublisher:
    """Stands in for TurnEventPublisher: records every published payload."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
def fake_publisher(monkeypatch: pytest.MonkeyPatch) -> _FakePublisher:
    """Replace publisher_from_activity so the observer's real __aenter__/__aexit__ — and thus
    the model-interaction bracket and its drain — run against an in-memory sink."""
    pub = _FakePublisher()

    @asynccontextmanager
    async def _fake_publisher_from_activity(context: Any, **_kw: Any):
        yield pub

    monkeypatch.setattr(
        AgentWorkflowRunner,
        "publisher_from_activity",
        staticmethod(_fake_publisher_from_activity),
    )
    return pub


def _ctx() -> TurnStreamContext:
    return TurnStreamContext(turn_id="t-1", turn_number=1, agent_id="agent-abc")


def _requested(pub: _FakePublisher) -> list[ToolRequested]:
    return [e for e in pub.events if isinstance(e, ToolRequested)]


# ---------------------------------------------------------------------------
# The Chat Completions backend: …arguments.done never arrives
# ---------------------------------------------------------------------------


def _chunk(
    tool_calls: list[dict[str, Any]] | None, finish: str | None
) -> ChatCompletionChunk:
    delta: dict[str, Any] = {}
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return ChatCompletionChunk.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
    )


def _tool_call(
    index: int, *, call_id: str | None = None, name: str | None = None, args: str = ""
) -> dict[str, Any]:
    fn: dict[str, Any] = {"arguments": args}
    if name is not None:
        fn["name"] = name
    out: dict[str, Any] = {"index": index, "function": fn}
    if call_id is not None:
        out["id"] = call_id
        out["type"] = "function"
    return out


class _ChunkStream:
    """Quacks like AsyncStream[ChatCompletionChunk]."""

    def __init__(self, chunks: list[ChatCompletionChunk]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for chunk in self._chunks:
            yield chunk


async def _drive_chat_completions(
    pub: _FakePublisher,
    chunks: list[ChatCompletionChunk],
    *,
    stop_after: int | None = None,
) -> list[Any]:
    """Feed chat chunks through the SDK's real reshaping handler into the observer.

    ``stop_after`` truncates the reshaped event stream after N events, standing in for a
    stream that ended mid-call (the handler itself always finishes what it starts).

    Returns the events published while the stream was still being consumed — i.e. BEFORE
    ``__aexit__`` and its drain ran. Tests assert against this snapshot to pin *when* a
    request was published: the drain is a backstop for a stream that died mid-call, so if
    it were the only thing publishing on this backend, every request would surface late,
    after the whole reply, rather than live where the model asked for it.
    """
    async with h.OpenAIStreamObserver(_ctx(), model="gpt-4o") as obs:
        seen = 0
        async for event in ChatCmplStreamHandler.handle_stream(
            Response.model_construct(output=[]),
            _ChunkStream(chunks),  # type: ignore[arg-type]
        ):
            if stop_after is not None and seen >= stop_after:
                break
            seen += 1
            await obs.on_event(event)
        return list(pub.events)


@pytest.mark.asyncio
async def test_chat_completions_backend_publishes_tool_requested(
    fake_publisher: _FakePublisher,
):
    # The whole bug in one test: this backend closes a function call with
    # response.output_item.done and NEVER emits response.function_call_arguments.done, so an
    # observer that only listens for the latter publishes nothing at all — on a fully
    # successful stream, for every tool call, on every Chat-Completions-backed model. The tool
    # still runs (run_tool publishes tool_start/tool_end), so the transcript shows a tool
    # starting and ending with no record the model ever asked for it, and no args. Nothing
    # republishes it: the workflow-side reducer emits no tool_requested.
    before_close = await _drive_chat_completions(
        fake_publisher,
        [
            _chunk([_tool_call(0, call_id="call_ONE", name="lookup")], None),
            _chunk([_tool_call(0, args='{"q": ')], None),
            _chunk([_tool_call(0, args='"cats"}')], None),
            _chunk(None, "tool_calls"),
        ],
    )

    # Published LIVE, off output_item.done, while the stream was still running — not swept up
    # late by the __aexit__ drain, which exists for a stream that dies mid-call.
    assert [type(e) for e in before_close].count(ToolRequested) == 1

    requested = _requested(fake_publisher)
    assert len(requested) == 1
    # tool_id is the SDK call_id, not the synthesized item id — it has to match the id
    # run_tool brackets the execution with, or the console cannot correlate them.
    assert requested[0].tool_id == "call_ONE"
    assert requested[0].tool_name == "lookup"
    # The streamed arg fragments are consolidated, not dropped: they are what a console
    # renders and what an approval prompt would show.
    assert requested[0].tool_input == {"q": "cats"}


@pytest.mark.asyncio
async def test_parallel_chat_completions_calls_stay_separate(
    fake_publisher: _FakePublisher,
):
    # This backend labels every synthesized item `__fake_id__`, so keying staged state by item
    # id would collide: two calls would overwrite each other's name/call_id and concatenate
    # their argument fragments into one unparseable blob. output_index keeps them apart.
    before_close = await _drive_chat_completions(
        fake_publisher,
        [
            _chunk([_tool_call(0, call_id="call_ONE", name="lookup")], None),
            _chunk([_tool_call(0, args='{"q": "cats"}')], None),
            _chunk([_tool_call(1, call_id="call_TWO", name="weather")], None),
            _chunk([_tool_call(1, args='{"city": "NYC"}')], None),
            _chunk(None, "tool_calls"),
        ],
    )

    assert [type(e) for e in before_close].count(ToolRequested) == 2

    requested = _requested(fake_publisher)
    assert [(r.tool_id, r.tool_name, r.tool_input) for r in requested] == [
        ("call_ONE", "lookup", {"q": "cats"}),
        ("call_TWO", "weather", {"city": "NYC"}),
    ]


@pytest.mark.asyncio
async def test_chat_completions_call_survives_stream_ending_before_its_terminal(
    fake_publisher: _FakePublisher,
):
    # Same hazard the Gemini plugin had: the call is staged, its terminal never arrives, and
    # __aexit__ still closes the bracket. Truncate right after both arg deltas — the reshaped
    # stream is created, output_item.added, delta, delta, output_item.done, completed — so
    # cutting at 4 stops just short of the terminal and the drain has to cover it.
    before_close = await _drive_chat_completions(
        fake_publisher,
        [
            _chunk([_tool_call(0, call_id="call_CUT", name="lookup")], None),
            _chunk([_tool_call(0, args='{"q": ')], None),
            _chunk([_tool_call(0, args='"cats"}')], None),
            _chunk(None, "tool_calls"),
        ],
        stop_after=4,
    )

    # Nothing had been published live — the terminal never arrived — so this one is entirely
    # the drain's doing, which is the opposite of the two tests above.
    assert ToolRequested not in [type(e) for e in before_close]

    requested = _requested(fake_publisher)
    assert len(requested) == 1
    assert requested[0].tool_id == "call_CUT"
    assert requested[0].tool_input == {"q": "cats"}


# ---------------------------------------------------------------------------
# The Responses backend: two terminals fire, so exactly-once has to hold
# ---------------------------------------------------------------------------


def _added(index: int, item_id: str, call_id: str, name: str) -> ResponseOutputItemAddedEvent:
    item = ResponseFunctionToolCall.model_construct(
        type="function_call", id=item_id, call_id=call_id, name=name, arguments=""
    )
    return ResponseOutputItemAddedEvent.model_construct(
        type="response.output_item.added", item=item, output_index=index
    )


def _args_delta(
    index: int, item_id: str, delta: str
) -> ResponseFunctionCallArgumentsDeltaEvent:
    return ResponseFunctionCallArgumentsDeltaEvent.model_construct(
        type="response.function_call_arguments.delta",
        item_id=item_id,
        output_index=index,
        delta=delta,
    )


def _args_done(
    index: int, item_id: str, name: str, arguments: str
) -> ResponseFunctionCallArgumentsDoneEvent:
    return ResponseFunctionCallArgumentsDoneEvent.model_construct(
        type="response.function_call_arguments.done",
        item_id=item_id,
        output_index=index,
        name=name,
        arguments=arguments,
    )


def _item_done(
    index: int, item_id: str, call_id: str, name: str, arguments: str
) -> ResponseOutputItemDoneEvent:
    item = ResponseFunctionToolCall.model_construct(
        type="function_call", id=item_id, call_id=call_id, name=name, arguments=arguments
    )
    return ResponseOutputItemDoneEvent.model_construct(
        type="response.output_item.done", item=item, output_index=index
    )


async def _drive(events: list[Any], *, raises: Exception | None = None) -> None:
    async with h.OpenAIStreamObserver(_ctx(), model="gpt-5.1") as obs:
        for event in events:
            await obs.on_event(event)
        if raises is not None:
            raise raises


@pytest.mark.asyncio
async def test_responses_backend_publishes_tool_requested_exactly_once(
    fake_publisher: _FakePublisher,
):
    # The complement of the fix, and the risk it introduces: the Responses backend emits BOTH
    # …arguments.done AND …output_item.done for one call, so listening to both must not trade
    # the silent omission for a silent duplicate. Nothing else emits tool_requested, so a
    # duplicate here would be a duplicate in the durable transcript.
    await _drive(
        [
            _added(0, "fc_1", "call_XYZ", "lookup"),
            _args_delta(0, "fc_1", '{"q": "cats"}'),
            _args_done(0, "fc_1", "lookup", '{"q": "cats"}'),
            _item_done(0, "fc_1", "call_XYZ", "lookup", '{"q": "cats"}'),
        ]
    )

    requested = _requested(fake_publisher)
    assert len(requested) == 1
    assert requested[0].tool_id == "call_XYZ"
    assert requested[0].tool_input == {"q": "cats"}


@pytest.mark.asyncio
async def test_drain_does_not_republish_a_call_that_already_closed(
    fake_publisher: _FakePublisher,
):
    # The drain runs on every close, including the clean one. A call whose terminal DID arrive
    # was already popped, so the drain has nothing to say about it.
    await _drive(
        [
            _added(0, "fc_1", "call_XYZ", "lookup"),
            _args_done(0, "fc_1", "lookup", '{"q": "cats"}'),
        ]
    )

    assert len(_requested(fake_publisher)) == 1


@pytest.mark.asyncio
async def test_tool_requested_survives_a_stream_that_raises_mid_call(
    fake_publisher: _FakePublisher,
):
    # Same hazard on the error path. The observer still re-raises (it never swallows a stream
    # error), but the bracket it closes now carries what the model had already requested.
    with pytest.raises(RuntimeError, match="upstream exploded"):
        await _drive(
            [
                _added(0, "fc_1", "call_ERR", "lookup"),
                _args_delta(0, "fc_1", '{"q": 1}'),
            ],
            raises=RuntimeError("upstream exploded"),
        )

    requested = _requested(fake_publisher)
    assert len(requested) == 1
    assert requested[0].tool_id == "call_ERR"
    assert requested[0].tool_input == {"q": 1}


@pytest.mark.asyncio
async def test_drained_tool_requested_stays_inside_the_model_interaction_bracket(
    fake_publisher: _FakePublisher,
):
    # Ordering matters as much as delivery: started → tools → ended, so a consumer can place
    # the drained request in the interaction it belongs to.
    await _drive([_added(0, "fc_1", "call_XYZ", "lookup")])

    assert [type(e) for e in fake_publisher.events] == [
        ModelInteractionStarted,
        ToolRequested,
        ModelInteractionEnded,
    ]
