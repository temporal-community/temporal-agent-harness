# ABOUTME: Unit-tests the Pydantic AI live translator (PydanticAIStreamObserver) and tool adapter in
# isolation — replays a synthetic sequence of raw Pydantic AI model-stream events (PartStart /
# PartDelta / PartEnd) through the observer with a fake in-memory publisher (no Temporal server, no
# API key) and asserts the emitted harness turn-stream vocabulary: the model-interaction bracket,
# reply/thought deltas, a single consolidated tool_requested keyed by the SDK tool_call_id, and
# mapped token usage. Also checks that as_pydantic_ai_tool / build_harness_toolset derive the right
# schema + activity-skip config from a harness tool.
#
# Run with: uv run pytest tests/ai_sdks/pydantic_ai/test_stream_observer.py -v

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from pydantic_ai.messages import (
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
)
from pydantic_ai.usage import RequestUsage

from temporal_agent_harness.ai_sdks import pydantic_ai_harness as h
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import (
    AgentEventType,
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    ThoughtSummaryDelta,
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
    """Replace publisher_from_activity so the observer's real __aenter__/__aexit__
    (and thus the model-interaction bracket) run against an in-memory sink."""
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


def _text_start(index: int, content: str) -> PartStartEvent:
    return PartStartEvent(index=index, part=TextPart(content=content))


def _text_delta(index: int, content: str) -> PartDeltaEvent:
    return PartDeltaEvent(index=index, delta=TextPartDelta(content_delta=content))


def _thinking_start(index: int, content: str) -> PartStartEvent:
    return PartStartEvent(index=index, part=ThinkingPart(content=content))


def _thinking_delta(index: int, content: str) -> PartDeltaEvent:
    return PartDeltaEvent(index=index, delta=ThinkingPartDelta(content_delta=content))


def _tool_start(index: int, call_id: str, name: str) -> PartStartEvent:
    return PartStartEvent(
        index=index, part=ToolCallPart(tool_name=name, args="", tool_call_id=call_id)
    )


def _tool_args_delta(index: int, call_id: str, fragment: str) -> PartDeltaEvent:
    return PartDeltaEvent(
        index=index,
        delta=ToolCallPartDelta(args_delta=fragment, tool_call_id=call_id),
    )


def _tool_end(index: int, call_id: str, name: str, args: str) -> PartEndEvent:
    return PartEndEvent(
        index=index, part=ToolCallPart(tool_name=name, args=args, tool_call_id=call_id)
    )


def _usage() -> RequestUsage:
    return RequestUsage(
        input_tokens=11,
        output_tokens=22,
        cache_read_tokens=5,
        details={"reasoning_tokens": 7},
    )


async def _drive(
    events: list[Any],
    context: TurnStreamContext,
    *,
    model: str | None = "openai:gpt-5.1",
    usage: RequestUsage | None = None,
) -> None:
    async with h.PydanticAIStreamObserver(context, model=model) as obs:
        for event in events:
            await obs.on_event(event)
        if usage is not None:
            obs.set_usage(usage)


@pytest.mark.asyncio
async def test_full_turn_translates_to_harness_vocabulary(fake_publisher: _FakePublisher):
    ctx = TurnStreamContext(turn_id="t-1", turn_number=1, agent_id="agent-abc")
    events = [
        _text_start(0, "Hel"),
        _text_delta(0, "lo"),
        _thinking_start(1, "thinking..."),
        _thinking_delta(1, " more"),
        _tool_start(2, "call_XYZ", "lookup"),
        _tool_args_delta(2, "call_XYZ", '{"q":'),
        _tool_args_delta(2, "call_XYZ", ' "cats"}'),
        _tool_end(2, "call_XYZ", "lookup", '{"q": "cats"}'),
    ]

    await _drive(events, ctx, usage=_usage())

    published = fake_publisher.events
    # Bracket: exactly one started first (naming the requested model, emitted at dispatch), one last.
    assert isinstance(published[0], ModelInteractionStarted)
    assert published[0].model == "openai:gpt-5.1"
    assert isinstance(published[-1], ModelInteractionEnded)

    starts = [e for e in published if isinstance(e, ModelInteractionStarted)]
    ends = [e for e in published if isinstance(e, ModelInteractionEnded)]
    assert len(starts) == 1 and len(ends) == 1

    # Reply text streamed through verbatim, in order: the TextPart's initial content on its
    # PartStart, then each TextPartDelta fragment.
    replies = [e for e in published if isinstance(e, ReplyDelta)]
    assert [r.text for r in replies] == ["Hel", "lo"]

    # Thinking surfaced from both the ThinkingPart start and its delta, payload preserved.
    thoughts = [e for e in published if isinstance(e, ThoughtSummaryDelta)]
    assert len(thoughts) == 2
    assert thoughts[0].delta.get("content") == "thinking..."
    assert thoughts[1].delta.get("content_delta") == " more"

    # Exactly one consolidated tool_requested at PartEnd, keyed by the SDK tool_call_id, with the
    # complete args from the finished part.
    requested = [e for e in published if isinstance(e, ToolRequested)]
    assert len(requested) == 1
    assert requested[0].tool_id == "call_XYZ"
    assert requested[0].tool_name == "lookup"
    assert requested[0].tool_input == {"q": "cats"}

    # Ended carries the model + mapped usage.
    ended = ends[0]
    assert ended.model == "openai:gpt-5.1"
    assert ended.usage is not None
    assert ended.usage.input_tokens == 11
    assert ended.usage.output_tokens == 22
    assert ended.usage.total_tokens == 33  # UsageBase.total_tokens == input + output
    assert ended.usage.cached_tokens == 5
    assert ended.usage.thought_tokens == 7
    assert ended.usage.tool_use_tokens is None


@pytest.mark.asyncio
async def test_tool_requested_flushed_on_close_without_part_end(fake_publisher: _FakePublisher):
    # Defensive path: a tool-call part that opened (and streamed args) but whose PartEnd never
    # arrived is still flushed as one tool_requested when the observer closes.
    ctx = TurnStreamContext(turn_id="t-2", turn_number=1, agent_id="agent-abc")
    events = [
        _tool_start(0, "call_BUF", "search"),
        _tool_args_delta(0, "call_BUF", '{"n": 42}'),
    ]
    await _drive(events, ctx)

    requested = [e for e in fake_publisher.events if isinstance(e, ToolRequested)]
    assert len(requested) == 1
    assert requested[0].tool_id == "call_BUF"
    assert requested[0].tool_name == "search"


@pytest.mark.asyncio
async def test_started_emitted_at_dispatch_before_any_event(fake_publisher: _FakePublisher):
    # The started bracket must be published at __aenter__ — before any event is fed — so the
    # started→ended span measures true model-call latency (time-to-first-token included).
    ctx = TurnStreamContext(turn_id="t-4", turn_number=1, agent_id="agent-abc")
    async with h.PydanticAIStreamObserver(ctx, model="openai:gpt-5.1") as obs:
        assert len(fake_publisher.events) == 1
        started = fake_publisher.events[0]
        assert isinstance(started, ModelInteractionStarted)
        assert started.model == "openai:gpt-5.1"
        await obs.on_event(_text_start(0, "hi"))
    assert isinstance(fake_publisher.events[-1], ModelInteractionEnded)


@pytest.mark.asyncio
async def test_bracket_closes_even_with_no_content(fake_publisher: _FakePublisher):
    ctx = TurnStreamContext(turn_id="t-3", turn_number=1, agent_id="agent-abc")
    # model=None here to exercise the degraded path (requested model unknown), no usage recorded.
    await _drive([], ctx, model=None)
    kinds = [e.type for e in fake_publisher.events]
    assert kinds == [
        AgentEventType.MODEL_INTERACTION_STARTED,
        AgentEventType.MODEL_INTERACTION_ENDED,
    ]
    ended = fake_publisher.events[-1]
    # No usage recorded -> stays None (e.g. stream errored before completing).
    assert ended.usage is None and ended.model is None


# ---------------------------------------------------------------------------
# Tool adapter
# ---------------------------------------------------------------------------


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city. `city` is a plain city name, e.g. "Paris"."""
    return f"It's 72°F and sunny in {city}."


def test_as_pydantic_ai_tool_derives_schema_from_harness_tool():
    tool = h.as_pydantic_ai_tool(get_weather)
    assert tool.name == "get_weather"
    assert "weather" in (tool.description or "").lower()
    schema = tool.function_schema.json_schema
    assert "city" in schema.get("properties", {})
    # Executes with a RunContext (harness owns the run via run_tool).
    assert tool.takes_ctx is True


def test_build_harness_toolset_returns_toolset_and_skip_config():
    toolset, tool_config = h.build_harness_toolset([get_weather], id="hello-tools")
    assert toolset.id == "hello-tools"
    # The activity wrapper is disabled for every adapted tool so it runs in-workflow.
    assert tool_config == {"hello-tools": {"get_weather": False}}


def test_as_pydantic_ai_tool_rejects_non_harness_tool():
    async def plain(x: int) -> int:
        return x

    with pytest.raises(TypeError):
        h.as_pydantic_ai_tool(plain)


class _FakeRunner(AgentWorkflowRunner):
    """A test double for AgentWorkflowRunner: a real subclass (so it passes the adapter's
    isinstance check and HarnessDeps' field validation) with a no-op __init__ that skips the heavy
    workflow-bound construction, recording how the adapted tool invokes run_tool."""

    def __init__(self, stream_context: TurnStreamContext | None = None) -> None:
        self.calls: list[Any] = []
        self._stream_context = stream_context

    @property
    def current_stream_context(self) -> TurnStreamContext | None:
        return self._stream_context

    async def run_tool(self, call_id: str, tool_callable: Any, /, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((call_id, tool_callable, args, kwargs))
        return "runner-result"


class _FakeCtx:
    """Minimal RunContext stand-in: only the fields the adapter reads."""

    def __init__(self, deps: Any, tool_call_id: str) -> None:
        self.deps = deps
        self.tool_call_id = tool_call_id


@pytest.mark.asyncio
async def test_tool_reads_runner_off_deps_not_workflow_instance():
    # The whole point of the fix: the runner is threaded EXPLICITLY via deps at the run(...) call
    # site — never assumed off workflow.instance()._runner. Prove the adapter invokes run_tool on
    # the runner it finds on ctx.deps, with the SDK tool_call_id as the correlation id.
    runner = _FakeRunner()
    tool = h.as_pydantic_ai_tool(get_weather)
    ctx = _FakeCtx(deps=h.HarnessDeps(runner=runner), tool_call_id="call_ABC")

    result = await tool.function(ctx, city="Paris")

    assert result == "runner-result"
    assert len(runner.calls) == 1
    call_id, tool_callable, _args, kwargs = runner.calls[0]
    assert call_id == "call_ABC"
    assert tool_callable is get_weather
    assert kwargs["city"] == "Paris"
    assert kwargs["injections"] is None


@pytest.mark.asyncio
async def test_tool_raises_loudly_when_no_runner_on_deps():
    # No runner on deps (e.g. wrongly run inside an activity, or deps didn't carry the runner) is a
    # developer misconfiguration, so it fails loudly with guidance rather than being papered over.
    tool = h.as_pydantic_ai_tool(get_weather)
    ctx = _FakeCtx(deps=h.HarnessDeps(), tool_call_id="call_NONE")
    with pytest.raises(RuntimeError, match="no live AgentWorkflowRunner"):
        await tool.function(ctx, city="Paris")


def test_harness_deps_snapshots_stream_context_from_runner():
    # Ergonomics: pass just the runner; HarnessDeps captures the in-flight turn's stream context
    # from it (resolved workflow-side, since the model-activity handler that consumes it can't reach
    # the runner). The runner stays a live attribute but is excluded from serialization.
    ctx = TurnStreamContext(turn_id="t-9", turn_number=3, agent_id="agent-abc")
    runner = _FakeRunner(stream_context=ctx)

    deps = h.HarnessDeps(runner=runner)
    assert deps.harness_stream_context == ctx
    assert deps.runner is runner
    # Serialized form drops the runner and keeps the snapshotted context.
    assert "runner" not in deps.model_dump()
    assert deps.model_dump()["harness_stream_context"]["turn_id"] == "t-9"

    # An explicit context overrides the snapshot rather than being clobbered.
    other = TurnStreamContext(turn_id="t-override", turn_number=1, agent_id="agent-abc")
    assert h.HarnessDeps(runner=runner, harness_stream_context=other).harness_stream_context == other
