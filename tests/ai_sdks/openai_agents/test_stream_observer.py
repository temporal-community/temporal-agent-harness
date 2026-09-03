# ABOUTME: Unit-tests the OpenAI Agents live translator (OpenAIStreamObserver) in isolation —
# replays a synthetic sequence of raw OpenAI Responses stream events through the observer with a
# fake in-memory publisher (no Temporal server, no OPENAI_API_KEY) and asserts the emitted harness
# turn-stream vocabulary: the model-interaction bracket, reply/thought/annotation deltas, and a
# single consolidated tool_requested keyed by the SDK call_id. This is the replay half of the
# conformance plan; the recorded-fixture half (real event shapes) is layered on separately.
#
# Run with: uv run pytest tests/ai_sdks/openai_agents/test_stream_observer.py -v

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest

from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemAddedEvent,
    ResponseOutputTextAnnotationAddedEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseTextDeltaEvent,
)
from openai.types.responses.response import Response
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
    ResponseUsage,
)

from temporal_agent_harness.ai_sdks import openai_agents_harness as h
from temporal_agent_harness.harness.agent_protocol import (
    AgentEventType,
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    TextAnnotationDelta,
    ThoughtSummaryDelta,
    ToolRequested,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
from temporal_agent_harness.harness.stream_context import TurnStreamContext


class _FakePublisher:
    """Stands in for TurnEventPublisher: records every published payload.

    Also records the kwargs each ``publisher_from_activity`` open was given, so a test
    can assert the flush cadence the observer asked for (see ``opened_with``).
    """

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.opened_with: list[dict[str, Any]] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
def fake_publisher(monkeypatch: pytest.MonkeyPatch) -> _FakePublisher:
    """Replace publisher_from_activity so the observer's real __aenter__/__aexit__
    (and thus the model-interaction bracket) run against an in-memory sink."""
    pub = _FakePublisher()

    @asynccontextmanager
    async def _fake_publisher_from_activity(context: Any, **kw: Any):
        pub.opened_with.append(kw)
        yield pub

    monkeypatch.setattr(
        AgentWorkflowRunner,
        "publisher_from_activity",
        staticmethod(_fake_publisher_from_activity),
    )
    return pub


def _text_delta(text: str) -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent.model_construct(
        type="response.output_text.delta", delta=text, item_id="msg_1"
    )


def _reasoning_delta(text: str) -> ResponseReasoningSummaryTextDeltaEvent:
    return ResponseReasoningSummaryTextDeltaEvent.model_construct(
        type="response.reasoning_summary_text.delta", delta=text, item_id="rs_1"
    )


def _annotation_added() -> ResponseOutputTextAnnotationAddedEvent:
    return ResponseOutputTextAnnotationAddedEvent.model_construct(
        type="response.output_text.annotation.added",
        annotation={"type": "url_citation", "url": "https://example.com"},
        item_id="msg_1",
    )


# `output_index` is a required field on all three of these real events and is what correlates
# them (item ids are unreliable — the Chat Completions backend reuses one synthesized id for
# every item), so the fixtures carry it explicitly: `model_construct` skips validation and would
# otherwise leave it None, quietly correlating on nothing.


def _fn_call_added(
    item_id: str, call_id: str, name: str, *, index: int = 0
) -> ResponseOutputItemAddedEvent:
    item = ResponseFunctionToolCall.model_construct(
        type="function_call", id=item_id, call_id=call_id, name=name, arguments=""
    )
    return ResponseOutputItemAddedEvent.model_construct(
        type="response.output_item.added", item=item, output_index=index
    )


def _args_delta(
    item_id: str, delta: str, *, index: int = 0
) -> ResponseFunctionCallArgumentsDeltaEvent:
    return ResponseFunctionCallArgumentsDeltaEvent.model_construct(
        type="response.function_call_arguments.delta",
        item_id=item_id,
        output_index=index,
        delta=delta,
    )


def _args_done(
    item_id: str, name: str, arguments: str, *, index: int = 0
) -> ResponseFunctionCallArgumentsDoneEvent:
    return ResponseFunctionCallArgumentsDoneEvent.model_construct(
        type="response.function_call_arguments.done",
        item_id=item_id,
        output_index=index,
        name=name,
        arguments=arguments,
    )


def _completed() -> ResponseCompletedEvent:
    usage = ResponseUsage.model_construct(
        input_tokens=11,
        output_tokens=22,
        total_tokens=33,
        input_tokens_details=InputTokensDetails.model_construct(cached_tokens=5),
        output_tokens_details=OutputTokensDetails.model_construct(reasoning_tokens=7),
    )
    response = Response.model_construct(usage=usage)
    return ResponseCompletedEvent.model_construct(
        type="response.completed", response=response
    )


async def _drive(
    events: list[Any],
    context: TurnStreamContext,
    *,
    model: str | None = "gpt-5.1",
    publisher: _FakePublisher | None = None,
) -> list[Any]:
    """Feed ``events`` through the observer.

    Returns what ``publisher`` had recorded before ``__aexit__`` ran, so a caller can tell a
    request published live by its terminal from one swept up by the closing drain. Both end up in
    the same final list, so without this a terminal that stopped firing would be invisible — the
    request would just arrive after the whole reply instead of where the model made it.
    """
    async with h.OpenAIStreamObserver(context, model=model) as obs:
        for event in events:
            await obs.on_event(event)
        return list(publisher.events) if publisher is not None else []


@pytest.mark.asyncio
async def test_full_turn_translates_to_harness_vocabulary(fake_publisher: _FakePublisher):
    ctx = TurnStreamContext(turn_id="t-1", turn_number=1, agent_id="agent-abc")
    events = [
        _text_delta("Hel"),
        _text_delta("lo"),
        _reasoning_delta("thinking..."),
        _annotation_added(),
        _fn_call_added("fc_item_1", "call_XYZ", "lookup"),
        _args_delta("fc_item_1", '{"q":'),
        _args_delta("fc_item_1", ' "cats"}'),
        _args_done("fc_item_1", "lookup", '{"q": "cats"}'),
        _completed(),
    ]

    before_close = await _drive(events, ctx, publisher=fake_publisher)

    published = fake_publisher.events
    # Bracket: exactly one started first (naming the requested model, emitted at dispatch
    # so the span times the whole call), one ended last.
    assert isinstance(published[0], ModelInteractionStarted)
    assert published[0].model == "gpt-5.1"
    assert isinstance(published[-1], ModelInteractionEnded)

    starts = [e for e in published if isinstance(e, ModelInteractionStarted)]
    ends = [e for e in published if isinstance(e, ModelInteractionEnded)]
    assert len(starts) == 1 and len(ends) == 1

    # Reply text streamed through verbatim, in order.
    replies = [e for e in published if isinstance(e, ReplyDelta)]
    assert [r.text for r in replies] == ["Hel", "lo"]

    # Thought summary + annotation each surfaced once, payload preserved.
    thoughts = [e for e in published if isinstance(e, ThoughtSummaryDelta)]
    assert len(thoughts) == 1 and thoughts[0].delta["delta"] == "thinking..."
    annos = [e for e in published if isinstance(e, TextAnnotationDelta)]
    assert len(annos) == 1 and annos[0].delta["annotation"]["url"] == "https://example.com"

    # Exactly one consolidated tool_requested, keyed by the SDK call_id, with
    # arguments assembled from the streamed fragments.
    requested = [e for e in published if isinstance(e, ToolRequested)]
    assert len(requested) == 1
    assert requested[0].tool_id == "call_XYZ"
    assert requested[0].tool_name == "lookup"
    assert requested[0].tool_input == {"q": "cats"}

    # Published LIVE, by the …arguments.done terminal, while the stream was still running. This
    # sequence carries no trailing …output_item.done (the Responses backend's second terminal), so
    # if arguments.done stopped publishing, only the closing drain could — and every assertion
    # above would still hold, with the request arriving after the whole reply.
    assert [type(e) for e in before_close].count(ToolRequested) == 1

    # Ended carries the model + mapped usage read off response.completed.
    ended = ends[0]
    assert ended.model == "gpt-5.1"
    assert ended.usage is not None
    assert ended.usage.input_tokens == 11
    assert ended.usage.output_tokens == 22
    assert ended.usage.total_tokens == 33
    assert ended.usage.cached_tokens == 5
    assert ended.usage.thought_tokens == 7
    assert ended.usage.tool_use_tokens is None


@pytest.mark.asyncio
async def test_tool_requested_falls_back_to_buffer_when_done_args_empty(
    fake_publisher: _FakePublisher,
):
    ctx = TurnStreamContext(turn_id="t-2", turn_number=1, agent_id="agent-abc")
    events = [
        _fn_call_added("fc_item_9", "call_BUF", "search"),
        _args_delta("fc_item_9", '{"n": '),
        _args_delta("fc_item_9", "42}"),
        _args_done("fc_item_9", "search", ""),  # done event carries no arguments
        _completed(),
    ]
    await _drive(events, ctx)

    requested = [e for e in fake_publisher.events if isinstance(e, ToolRequested)]
    assert len(requested) == 1
    assert requested[0].tool_id == "call_BUF"
    assert requested[0].tool_input == {"n": 42}


@pytest.mark.asyncio
async def test_started_emitted_at_dispatch_before_any_event(fake_publisher: _FakePublisher):
    # The started bracket must be published at __aenter__ — before any event is fed — so
    # the started→ended span measures the true model-call latency (time-to-first-token
    # included), not just the tail after the first chunk arrives. Prove it by opening the
    # observer and checking started is already published before on_event is ever called.
    ctx = TurnStreamContext(turn_id="t-4", turn_number=1, agent_id="agent-abc")
    async with h.OpenAIStreamObserver(ctx, model="gpt-5.1") as obs:
        assert len(fake_publisher.events) == 1
        started = fake_publisher.events[0]
        assert isinstance(started, ModelInteractionStarted)
        assert started.model == "gpt-5.1"
        # Only now does the first event arrive.
        await obs.on_event(_text_delta("hi"))
    assert isinstance(fake_publisher.events[-1], ModelInteractionEnded)


@pytest.mark.asyncio
async def test_bracket_closes_even_with_no_content(fake_publisher: _FakePublisher):
    ctx = TurnStreamContext(turn_id="t-3", turn_number=1, agent_id="agent-abc")
    # model=None here to exercise the degraded path (requested model unknown).
    await _drive([], ctx, model=None)
    kinds = [e.type for e in fake_publisher.events]
    assert kinds == [
        AgentEventType.MODEL_INTERACTION_STARTED,
        AgentEventType.MODEL_INTERACTION_ENDED,
    ]
    ended = fake_publisher.events[-1]
    # No response.completed seen -> usage stays None (e.g. stream errored early).
    assert ended.usage is None and ended.model is None


# ---------------------------------------------------------------------------
# Flush cadence: the plugin's streaming_batch_interval reaches the publisher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_factory_threads_batch_interval_to_the_publisher(
    fake_publisher: _FakePublisher,
):
    # ModelActivityParameters.streaming_batch_interval is handed to the factory by
    # select_observer; it must reach the turn publisher, or configuring the field would
    # silently do nothing on the harness path (each flush is one Signal, so this is the
    # knob that trades UI snappiness for workflow-history volume).
    ctx = TurnStreamContext(turn_id="t-5", turn_number=1, agent_id="agent-abc")
    token = h._HarnessStreamToken(context=ctx, model="gpt-5.1")

    observer = h.harness_observer_factory(
        token.model_dump(mode="json"), batch_interval=timedelta(milliseconds=750)
    )
    async with observer:
        pass

    assert fake_publisher.opened_with == [{"batch_interval": timedelta(milliseconds=750)}]


@pytest.mark.asyncio
async def test_factory_without_interval_defers_to_the_harness_default(
    fake_publisher: _FakePublisher,
):
    # Called by hand (no cadence): pass None through, so publisher_from_activity applies
    # DEFAULT_PUBLISH_BATCH_INTERVAL rather than the observer duplicating that value.
    ctx = TurnStreamContext(turn_id="t-6", turn_number=1, agent_id="agent-abc")
    token = h._HarnessStreamToken(context=ctx, model="gpt-5.1")

    async with h.harness_observer_factory(token):
        pass

    assert fake_publisher.opened_with == [{"batch_interval": None}]
