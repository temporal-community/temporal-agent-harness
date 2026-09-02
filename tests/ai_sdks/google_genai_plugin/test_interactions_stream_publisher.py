# ABOUTME: Tests that the Gemini Interactions activity's live translator does not silently drop
# tool events when a stream ends (or errors) mid-step. Drives the real activity with a fake
# genai.Client whose SSE stream stops without the trailing step.stop, against an in-memory
# publisher (no Temporal server, no API key), and asserts the staged tool_requested / tool_start /
# tool_end are still published — inside the model-interaction bracket, before the ended.
#
# Run with: uv run pytest tests/ai_sdks/google_genai_plugin/test_interactions_stream_publisher.py -v

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from google.genai._interactions.types import (
    FileSearchCallStep,
    FileSearchResultStep,
    FunctionCallStep,
    StepDelta,
    StepStart,
    StepStop,
)
from google.genai._interactions.types.step_delta import DeltaArgumentsDelta

from temporal_agent_harness.ai_sdks.google_genai_plugin._interactions_activity import (
    make_gemini_interactions_create_streamed,
)
from temporal_agent_harness.harness.agent_protocol import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    ToolEndEvent,
    ToolRequested,
    ToolStartEvent,
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
    """Replace publisher_from_activity so the activity's real bracket + drain run against an
    in-memory sink."""
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


# --- synthetic SSE events ---------------------------------------------------


def _function_call_start(index: int, call_id: str, name: str) -> StepStart:
    return StepStart(
        event_type="step.start",
        index=index,
        step=FunctionCallStep(
            id=call_id, name=name, arguments={}, type="function_call"
        ),
    )


def _args_delta(index: int, fragment: str) -> StepDelta:
    return StepDelta(
        event_type="step.delta",
        index=index,
        delta=DeltaArgumentsDelta(type="arguments_delta", arguments=fragment),
    )


def _file_search_call_start(index: int, call_id: str) -> StepStart:
    return StepStart(
        event_type="step.start",
        index=index,
        step=FileSearchCallStep(id=call_id, type="file_search_call"),
    )


def _file_search_result_start(index: int, call_id: str) -> StepStart:
    return StepStart(
        event_type="step.start",
        index=index,
        step=FileSearchResultStep(call_id=call_id, type="file_search_result"),
    )


def _step_stop(index: int) -> StepStop:
    return StepStop(event_type="step.stop", index=index)


class _FakeStream:
    """Quacks like the SDK's AsyncStream: yields the given events, then optionally raises."""

    def __init__(self, events: list[Any], *, raises: Exception | None = None) -> None:
        self._events = events
        self._raises = raises

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for event in self._events:
            yield event
        if self._raises is not None:
            raise self._raises


class _FakeClient:
    """Minimal genai.Client stand-in: ``client.aio.interactions.create(**kwargs)`` — the one
    call the activity makes — resolved through self as its own ``aio`` / ``interactions``."""

    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream
        self.aio = self
        self.interactions = self

    async def create(self, **_kwargs: Any) -> _FakeStream:
        return self._stream


async def _run(events: list[Any], *, raises: Exception | None = None) -> Any:
    activity = make_gemini_interactions_create_streamed(
        _FakeClient(_FakeStream(events, raises=raises))  # type: ignore[arg-type]
    )
    return await activity(
        {"model": "gemini-3-pro-preview"},
        TurnStreamContext(turn_id="t-1", turn_number=1, agent_id="agent-abc"),
    )


# --- the property: a stream ending mid-step must not lose that step's tool events ---


@pytest.mark.asyncio
async def test_custom_tool_requested_survives_stream_ending_mid_step(
    fake_publisher: _FakePublisher,
):
    # The model streamed a full function-call request but the stream ended before its step.stop.
    # Without the drain, tool_requested is staged and discarded while ModelInteractionEnded still
    # publishes — the transcript reads complete with the tool invocation silently missing.
    await _run(
        [
            _function_call_start(0, "call_ABC", "search"),
            _args_delta(0, '{"q": '),
            _args_delta(0, '"cats"}'),
        ]
    )

    requested = [e for e in fake_publisher.events if isinstance(e, ToolRequested)]
    assert len(requested) == 1
    assert requested[0].tool_id == "call_ABC"
    assert requested[0].tool_name == "search"
    # The buffered arg fragments are consolidated, not dropped: they are what a console renders.
    assert requested[0].tool_input == {"q": "cats"}


@pytest.mark.asyncio
async def test_drained_tool_events_stay_inside_the_model_interaction_bracket(
    fake_publisher: _FakePublisher,
):
    # Ordering matters as much as delivery: started → tools → ended, so a consumer can place the
    # drained events in the interaction they belong to.
    await _run([_function_call_start(0, "call_ABC", "search")])

    kinds = [type(e) for e in fake_publisher.events]
    assert kinds == [ModelInteractionStarted, ToolRequested, ModelInteractionEnded]


@pytest.mark.asyncio
async def test_builtin_tool_start_and_end_survive_stream_ending_mid_step(
    fake_publisher: _FakePublisher,
):
    # Built-in (server-side) tools are bracketed ONLY here — nothing on the workflow side
    # republishes them — so a stranded call/result step loses the whole invocation.
    await _run(
        [
            _file_search_call_start(0, "fs_1"),
            _step_stop(0),
            _file_search_result_start(1, "fs_1"),
        ]
    )

    starts = [e for e in fake_publisher.events if isinstance(e, ToolStartEvent)]
    ends = [e for e in fake_publisher.events if isinstance(e, ToolEndEvent)]
    assert [s.tool_id for s in starts] == ["fs_1"]
    assert [e.tool_id for e in ends] == ["fs_1"]


@pytest.mark.asyncio
async def test_tool_requested_survives_a_stream_that_raises_mid_step(
    fake_publisher: _FakePublisher,
):
    # Same hazard on the error path. The activity still fails (the drain never swallows the
    # error), but the bracket it closes now carries what the model had already requested.
    with pytest.raises(RuntimeError, match="upstream exploded"):
        await _run(
            [_function_call_start(0, "call_ERR", "search"), _args_delta(0, '{"q": 1}')],
            raises=RuntimeError("upstream exploded"),
        )

    requested = [e for e in fake_publisher.events if isinstance(e, ToolRequested)]
    assert len(requested) == 1
    assert requested[0].tool_id == "call_ERR"
    assert requested[0].tool_input == {"q": 1}
    assert isinstance(fake_publisher.events[-1], ModelInteractionEnded)


@pytest.mark.asyncio
async def test_drain_does_not_republish_a_step_that_already_stopped(
    fake_publisher: _FakePublisher,
):
    # The complement of the fix: never trade a silent omission for a silent duplicate. A step
    # whose stop DID arrive was already popped, so the drain has nothing to say about it.
    await _run(
        [
            _function_call_start(0, "call_ABC", "search"),
            _args_delta(0, '{"q": "cats"}'),
            _step_stop(0),
        ]
    )

    requested = [e for e in fake_publisher.events if isinstance(e, ToolRequested)]
    assert len(requested) == 1
    assert requested[0].tool_input == {"q": "cats"}
