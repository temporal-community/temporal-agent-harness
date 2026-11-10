# ABOUTME: Unit-tests the Strands live translator (StrandsStreamObserver) in isolation: replays raw
# Strands stream events through it with an in-memory publisher and checks the harness events it
# emits. Also checks harness_strands_agent's refusals, which need no worker.

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast

import pytest
from strands import tool as strands_tool
from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment

from temporal_agent_harness.ai_sdks import strands_agents_harness as h
from temporal_agent_harness.harness.agent_protocol import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    ThoughtSummaryDelta,
    TokenUsage,
    ToolRequested,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
from temporal_agent_harness.harness.stream_context import TurnStreamContext


class _FakePublisher:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.opened_with: list[dict[str, Any]] = []

    def publish(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
def fake_publisher(monkeypatch: pytest.MonkeyPatch) -> _FakePublisher:
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


_CONTEXT = TurnStreamContext(turn_id="t-1", turn_number=1, agent_id="agent-abc")


async def _replay(events: list[dict[str, Any]], *, model: str | None = "claude") -> None:
    token = h._HarnessStreamToken(context=_CONTEXT, model=model).model_dump(mode="json")
    async with h.harness_observer_factory(token, batch_interval=timedelta(milliseconds=50)) as obs:
        for event in events:
            await obs.on_event(event)


async def test_a_reply_streams_inside_the_model_interaction_bracket_with_its_usage(
    fake_publisher: _FakePublisher,
):
    await _replay(
        [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "Thinking"}}}},
            {"contentBlockDelta": {"delta": {"reasoningContent": {"signature": "sig"}}}},
            {"contentBlockDelta": {"delta": {"text": "Hello"}}},
            {"contentBlockDelta": {"delta": {"text": ", world"}}},
            {"contentBlockStop": {}},
            {"messageStop": {"stopReason": "end_turn"}},
            {
                "metadata": {
                    "usage": {
                        "inputTokens": 12,
                        "outputTokens": 5,
                        "totalTokens": 17,
                        "cacheReadInputTokens": 4,
                    },
                    "metrics": {"latencyMs": 1},
                }
            },
        ]
    )

    assert fake_publisher.events == [
        ModelInteractionStarted(model="claude"),
        ThoughtSummaryDelta(delta={"text": "Thinking"}),
        ThoughtSummaryDelta(delta={"signature": "sig"}),
        ReplyDelta(text="Hello"),
        ReplyDelta(text=", world"),
        ModelInteractionEnded(
            model="claude",
            usage=TokenUsage(input_tokens=12, output_tokens=5, total_tokens=17, cached_tokens=4),
        ),
    ]
    assert fake_publisher.opened_with == [{"batch_interval": timedelta(milliseconds=50)}]


async def test_a_tool_call_streamed_in_fragments_is_one_tool_requested_keyed_by_tool_use_id(
    fake_publisher: _FakePublisher,
):
    await _replay(
        [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "Let me check."}}},
            {"contentBlockStop": {}},
            {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "tu-1", "name": "get_weather"}}}},
            {"contentBlockDelta": {"delta": {"toolUse": {"input": '{"ci'}}}},
            {"contentBlockDelta": {"delta": {"toolUse": {"input": 'ty": "Paris"}'}}}},
            {"contentBlockStop": {}},
            {"contentBlockStart": {"start": {"toolUse": {"toolUseId": "tu-2", "name": "roll"}}}},
            {"contentBlockStop": {}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
    )

    requested = [e for e in fake_publisher.events if isinstance(e, ToolRequested)]
    assert requested == [
        ToolRequested(tool_id="tu-1", tool_name="get_weather", tool_input={"city": "Paris"}),
        ToolRequested(tool_id="tu-2", tool_name="roll", tool_input={}),
    ]


async def test_the_bracket_closes_when_the_stream_fails(fake_publisher: _FakePublisher):
    token = h._HarnessStreamToken(context=_CONTEXT, model="claude")
    with pytest.raises(RuntimeError, match="provider down"):
        async with h.harness_observer_factory(token) as obs:
            await obs.on_event({"contentBlockDelta": {"delta": {"text": "Hel"}}})
            raise RuntimeError("provider down")

    assert fake_publisher.events == [
        ModelInteractionStarted(model="claude"),
        ReplyDelta(text="Hel"),
        ModelInteractionEnded(model="claude", usage=None),
    ]


# The refusals happen before the runner is used, and a real runner needs a workflow.
_NO_RUNNER = cast(AgentWorkflowRunner, object())


@strands_tool
def _roll() -> int:
    """Roll a die."""
    return 4


def test_harness_strands_agent_refuses_a_strands_tool():
    with pytest.raises(TypeError, match="_roll is not a harness tool"):
        h.harness_strands_agent(_NO_RUNNER, model="m", tools=[_roll])


def test_harness_strands_agent_refuses_a_sandbox_that_is_not_a_temporal_sandbox():
    with pytest.raises(TypeError, match="must be a TemporalSandbox"):
        h.harness_strands_agent(_NO_RUNNER, model="m", sandbox=NotASandboxLocalEnvironment())


@pytest.mark.parametrize("key", ["streaming_topic", "stream_route"])
def test_harness_strands_agent_refuses_its_own_streaming_settings(key: str):
    with pytest.raises(ValueError, match=key):
        h.harness_strands_agent(_NO_RUNNER, model="m", **{key: None})


def test_as_strands_tool_refuses_a_callable_that_is_not_a_harness_tool():
    async def plain(city: str) -> str:
        return city

    with pytest.raises(TypeError, match="not a harness tool"):
        h.as_strands_tool(_NO_RUNNER, plain)
