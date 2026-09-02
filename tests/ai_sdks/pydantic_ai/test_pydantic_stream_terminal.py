# ABOUTME: Tests WHEN the Pydantic AI live translator publishes tool_requested, not just whether it
# eventually does. Drives the REAL pydantic_ai StreamedResponse — whose shared iterator_with_part_end
# is the single, provider-independent producer of PartEnd — so the claim "the terminal fires live for
# every provider" is evidence rather than inference. Snapshots what was published BEFORE __aexit__ to
# tell a live publish from a drain backstop, since a drain that always runs makes a never-firing
# terminal invisible. Also pins that the drain reports the arguments the model actually streamed.
#
# Run with: uv run pytest tests/ai_sdks/pydantic_ai/test_pydantic_stream_terminal.py -v

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import pytest

from pydantic_ai.messages import (
    ModelResponseStreamEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    ToolCallPart,
    ToolCallPartDelta,
)
from pydantic_ai.models import ModelRequestParameters, StreamedResponse

from temporal_agent_harness.ai_sdks import pydantic_ai_harness as h
from temporal_agent_harness.harness.agent_protocol import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
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
    """Replace publisher_from_activity so the observer's real __aenter__/__aexit__ — and thus the
    model-interaction bracket and its drain — run against an in-memory sink."""
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
# A real StreamedResponse whose provider-side iterator we script
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedStream(StreamedResponse):
    """A genuine ``StreamedResponse`` with a hand-scripted provider iterator.

    Subclassing the real base is the point: ``PartEnd`` is synthesized by
    ``StreamedResponse.__aiter__``'s shared ``iterator_with_part_end`` wrapper, never by a
    provider's ``_get_event_iterator``. Driving the real base therefore exercises the actual
    terminal-producing code every provider inherits, instead of a fixture asserting the PartEnd we
    already believe in. ``script`` entries are calls on the real parts manager, exactly as a
    provider's model class makes them.
    """

    script: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    raise_at_end: Exception | None = None

    @property
    def model_name(self) -> str:
        return "scripted-model"

    @property
    def provider_name(self) -> str:
        return "scripted"

    @property
    def provider_url(self) -> str:
        return "https://example.invalid"

    @property
    def timestamp(self) -> datetime:
        return datetime.now(timezone.utc)

    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        for kind, kwargs in self.script:
            if kind == "text":
                for event in self._parts_manager.handle_text_delta(**kwargs):
                    yield event
            elif kind == "tool":
                event = self._parts_manager.handle_tool_call_delta(**kwargs)
                if event is not None:
                    yield event
            else:  # pragma: no cover - guards the fixture itself
                raise AssertionError(f"unknown script entry {kind!r}")
        if self.raise_at_end is not None:
            raise self.raise_at_end


def _tool(vendor_id: str, **kwargs: Any) -> tuple[str, dict[str, Any]]:
    return "tool", {"vendor_part_id": vendor_id, **kwargs}


def _text(vendor_id: str, content: str) -> tuple[str, dict[str, Any]]:
    return "text", {"vendor_part_id": vendor_id, "content": content}


async def _drive_real_stream(
    pub: _FakePublisher,
    script: list[tuple[str, dict[str, Any]]],
    *,
    raise_at_end: Exception | None = None,
) -> list[Any]:
    """Run the observer over a real StreamedResponse; return what was published pre-``__aexit__``."""
    stream = _ScriptedStream(
        model_request_parameters=ModelRequestParameters(),
        script=script,
        raise_at_end=raise_at_end,
    )
    before_close: list[Any] = []
    async with h.PydanticAIStreamObserver(_ctx(), model="scripted-model") as obs:
        async for event in stream:
            await obs.on_event(event)
        before_close = list(pub.events)
    return before_close


@pytest.mark.asyncio
async def test_part_end_fires_live_so_the_drain_is_only_a_backstop(
    fake_publisher: _FakePublisher,
):
    # The clearance this integration rests on: PartEnd really does arrive on the live path, so
    # tool_requested is published DURING iteration rather than swept up at close. If the terminal
    # ever stopped firing, the drain would still publish and a "was it published" assertion would
    # not notice — the request would just arrive after the whole reply.
    before_close = await _drive_real_stream(
        fake_publisher,
        [_tool("a", tool_name="lookup", args='{"q": "cats"}', tool_call_id="call_1")],
    )

    assert [type(e) for e in before_close].count(ToolRequested) == 1
    requested = _requested(fake_publisher)
    assert len(requested) == 1
    assert requested[0].tool_id == "call_1"
    assert requested[0].tool_name == "lookup"
    assert requested[0].tool_input == {"q": "cats"}


@pytest.mark.asyncio
async def test_tool_requested_precedes_the_reply_text_that_follows_it(
    fake_publisher: _FakePublisher,
):
    # Ordering is the observable difference between a live publish and a drain. With a tool call
    # first and reply text after, a live publish puts tool_requested BEFORE the text; the drain
    # would put it after everything, misordering the transcript.
    before_close = await _drive_real_stream(
        fake_publisher,
        [
            _tool("a", tool_name="lookup", args='{"q": "cats"}', tool_call_id="call_1"),
            _text("t", "All done"),
        ],
    )

    kinds = [type(e) for e in before_close]
    assert kinds.index(ToolRequested) < kinds.index(ReplyDelta)


@pytest.mark.asyncio
async def test_parallel_tool_calls_each_get_their_own_live_terminal(
    fake_publisher: _FakePublisher,
):
    # iterator_with_part_end tracks only the LAST start event, closing a part when the next one
    # opens and the final one when the provider iterator drains. Two calls must still yield two
    # distinct requests, both live.
    before_close = await _drive_real_stream(
        fake_publisher,
        [
            _tool("a", tool_name="lookup", args='{"q": "cats"}', tool_call_id="call_1"),
            _tool("b", tool_name="weather", args='{"city": "NYC"}', tool_call_id="call_2"),
        ],
    )

    assert [type(e) for e in before_close].count(ToolRequested) == 2
    assert [(r.tool_id, r.tool_name, r.tool_input) for r in _requested(fake_publisher)] == [
        ("call_1", "lookup", {"q": "cats"}),
        ("call_2", "weather", {"city": "NYC"}),
    ]


@pytest.mark.asyncio
async def test_stream_that_raises_mid_call_still_reports_the_streamed_arguments(
    fake_publisher: _FakePublisher,
):
    # The backstop, exercised through the real base: when the provider iterator raises, the
    # post-loop PartEnd flush in __aiter__ is skipped, so only the drain can publish. It must
    # report the arguments the model actually streamed — a request recorded with tool_input={} is
    # silently wrong content, which a reader cannot distinguish from a genuinely argument-less call.
    with pytest.raises(RuntimeError, match="upstream exploded"):
        await _drive_real_stream(
            fake_publisher,
            [
                _tool("a", tool_name="lookup", args='{"q": ', tool_call_id="call_ERR"),
                _tool("a", args='"cats"}'),
            ],
            raise_at_end=RuntimeError("upstream exploded"),
        )

    requested = _requested(fake_publisher)
    assert len(requested) == 1
    assert requested[0].tool_id == "call_ERR"
    assert requested[0].tool_input == {"q": "cats"}
    # Still inside the bracket: the drain runs before the ended.
    assert isinstance(fake_publisher.events[-1], ModelInteractionEnded)


# ---------------------------------------------------------------------------
# Hand-built events: the drain's own argument fidelity and exactly-once
# ---------------------------------------------------------------------------


def _tool_start(index: int, call_id: str, name: str) -> PartStartEvent:
    return PartStartEvent(
        index=index, part=ToolCallPart(tool_name=name, args="", tool_call_id=call_id)
    )


def _tool_args_delta(index: int, call_id: str, fragment: str) -> PartDeltaEvent:
    return PartDeltaEvent(
        index=index, delta=ToolCallPartDelta(args_delta=fragment, tool_call_id=call_id)
    )


def _tool_end(index: int, call_id: str, name: str, args: str) -> PartEndEvent:
    return PartEndEvent(
        index=index, part=ToolCallPart(tool_name=name, args=args, tool_call_id=call_id)
    )


async def _drive(events: list[Any]) -> None:
    async with h.PydanticAIStreamObserver(_ctx(), model="scripted-model") as obs:
        for event in events:
            await obs.on_event(event)


@pytest.mark.asyncio
async def test_drain_consolidates_the_streamed_arg_fragments(
    fake_publisher: _FakePublisher,
):
    # The staged part is the PartStart snapshot, whose args are empty; the arguments only exist as
    # ToolCallPartDelta fragments. The live path never needed them (PartEnd carries the complete
    # part), so nothing folded them in — and the drain published a request with no arguments.
    await _drive(
        [
            _tool_start(0, "call_BUF", "search"),
            _tool_args_delta(0, "call_BUF", '{"n": '),
            _tool_args_delta(0, "call_BUF", "42}"),
        ]
    )

    requested = _requested(fake_publisher)
    assert len(requested) == 1
    assert requested[0].tool_id == "call_BUF"
    assert requested[0].tool_name == "search"
    assert requested[0].tool_input == {"n": 42}


@pytest.mark.asyncio
async def test_drain_does_not_republish_a_call_that_already_ended(
    fake_publisher: _FakePublisher,
):
    # The drain runs on every close, including the clean one. Never trade a silent omission for a
    # silent duplicate: nothing else emits tool_requested, so a duplicate here is a duplicate in
    # the durable transcript.
    await _drive(
        [
            _tool_start(0, "call_XYZ", "lookup"),
            _tool_args_delta(0, "call_XYZ", '{"q": "cats"}'),
            _tool_end(0, "call_XYZ", "lookup", '{"q": "cats"}'),
        ]
    )

    assert len(_requested(fake_publisher)) == 1


@pytest.mark.asyncio
async def test_drained_request_stays_inside_the_model_interaction_bracket(
    fake_publisher: _FakePublisher,
):
    # started → tools → ended, so a consumer can place the drained request in the interaction it
    # belongs to.
    await _drive([_tool_start(0, "call_XYZ", "lookup")])

    assert [type(e) for e in fake_publisher.events] == [
        ModelInteractionStarted,
        ToolRequested,
        ModelInteractionEnded,
    ]
