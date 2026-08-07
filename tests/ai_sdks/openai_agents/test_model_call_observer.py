# ABOUTME: Unit-tests the OpenAI Agents NON-streaming model-invocation bracket
# (OpenAIModelCallObserver + model_call_observer_provider) in isolation — drives the observer over a
# synthetic `ModelResponse` with a fake in-workflow runner (no Temporal server, no OPENAI_API_KEY)
# and asserts the harness turn-stream vocabulary a non-streamed turn must still produce: the
# model_interaction_started/ended span, its token accounting, and one tool_requested per function
# call the model asked for. Guards issue #50 — those are facts about the turn, so turning token
# streaming off must not drop them.
#
# Run with: uv run pytest tests/ai_sdks/openai_agents/test_model_call_observer.py -v

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agents.items import ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from temporal_agent_harness.ai_sdks import openai_agents_harness as h
from temporal_agent_harness.ai_sdks.integration_helpers import (
    NullModelCallObserver,
    select_model_call_observer,
)
from temporal_agent_harness.harness.agent_protocol import (
    AgentEventType,
    ModelInteractionEnded,
    ModelInteractionStarted,
    ToolRequested,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext


class _FakeRunner:
    """Stands in for the in-workflow AgentWorkflowRunner: records every published payload.

    Mirrors the two members the observer path touches — ``current_stream_context`` (the
    provider's "is a turn in flight?" gate) and ``publish``, which the real runner raises from
    when called between turns.
    """

    def __init__(self, context: TurnStreamContext | None) -> None:
        self.current_stream_context = context
        self.events: list[Any] = []

    def publish(self, event: Any) -> None:
        if self.current_stream_context is None:
            raise RuntimeError("publish() called with no active turn")
        self.events.append(event)


@pytest.fixture
def fake_runner(monkeypatch: pytest.MonkeyPatch) -> _FakeRunner:
    """A recording stand-in, swapped in for the provider's ``isinstance`` gate so the real
    provider → observer path runs end to end against an in-memory sink (no workflow, no
    Temporal server). The gate itself is covered separately by the "declines" tests."""
    monkeypatch.setattr(h, "AgentWorkflowRunner", _FakeRunner)
    return _FakeRunner(TurnStreamContext(turn_id="t-1", turn_number=1, agent_id="agent-abc"))


def _usage() -> Usage:
    return Usage(
        input_tokens=11,
        output_tokens=22,
        total_tokens=33,
        input_tokens_details=InputTokensDetails.model_construct(cached_tokens=5),
        output_tokens_details=OutputTokensDetails.model_construct(reasoning_tokens=7),
    )


def _fn_call(call_id: str, name: str, arguments: str) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall.model_construct(
        type="function_call", id=f"item_{call_id}", call_id=call_id, name=name, arguments=arguments
    )


def _message() -> ResponseOutputMessage:
    return ResponseOutputMessage.model_construct(
        type="message", id="msg_1", role="assistant", status="completed", content=[]
    )


def _response(output: list[Any], usage: Usage | None = None) -> ModelResponse:
    return ModelResponse(output=output, usage=usage or _usage(), response_id="resp_1")


def test_non_streamed_call_publishes_bracket_usage_and_tool_requests(fake_runner: _FakeRunner):
    obs = h.model_call_observer_provider("gpt-5.1", fake_runner)
    assert obs is not None

    with obs:
        # The span opens at dispatch, before the model activity is awaited — so it measures
        # the real model-call latency, exactly as the streamed path does.
        assert len(fake_runner.events) == 1
        assert isinstance(fake_runner.events[0], ModelInteractionStarted)
        obs.on_response(
            _response([_message(), _fn_call("call_XYZ", "lookup", '{"q": "cats"}')])
        )

    published = fake_runner.events
    assert isinstance(published[0], ModelInteractionStarted)
    assert published[0].model == "gpt-5.1"
    assert isinstance(published[-1], ModelInteractionEnded)

    starts = [e for e in published if isinstance(e, ModelInteractionStarted)]
    ends = [e for e in published if isinstance(e, ModelInteractionEnded)]
    assert len(starts) == 1 and len(ends) == 1

    # Token accounting survives non-streaming — the sharpest edge in issue #50.
    ended = ends[0]
    assert ended.model == "gpt-5.1"
    assert ended.usage is not None
    assert ended.usage.input_tokens == 11
    assert ended.usage.output_tokens == 22
    assert ended.usage.total_tokens == 33
    assert ended.usage.cached_tokens == 5
    assert ended.usage.thought_tokens == 7
    assert ended.usage.tool_use_tokens is None

    # One tool_requested per function call, keyed by the SDK call_id (the same id run_tool
    # uses), published BEFORE the bracket closes — matching the streamed ordering.
    requested = [e for e in published if isinstance(e, ToolRequested)]
    assert len(requested) == 1
    assert requested[0].tool_id == "call_XYZ"
    assert requested[0].tool_name == "lookup"
    assert requested[0].tool_input == {"q": "cats"}
    assert published.index(requested[0]) < published.index(ended)


def test_parallel_tool_calls_each_get_their_own_request(fake_runner: _FakeRunner):
    obs = h.model_call_observer_provider("gpt-5.1", fake_runner)
    assert obs is not None
    with obs:
        obs.on_response(
            _response(
                [
                    _fn_call("call_A", "get_coordinates", '{"city": "Paris"}'),
                    _fn_call("call_B", "get_weather", '{"lat": 1, "lon": 2}'),
                ]
            )
        )

    requested = [e for e in fake_runner.events if isinstance(e, ToolRequested)]
    assert [(r.tool_id, r.tool_name) for r in requested] == [
        ("call_A", "get_coordinates"),
        ("call_B", "get_weather"),
    ]
    assert requested[0].tool_input == {"city": "Paris"}


def test_bracket_closes_when_the_model_call_fails(fake_runner: _FakeRunner):
    # A failed model call must still close its started bracket (usage stays None, since no
    # response ever came back) and must not have its exception swallowed.
    obs = h.model_call_observer_provider(None, fake_runner)
    assert obs is not None
    with pytest.raises(RuntimeError, match="boom"):
        with obs:
            raise RuntimeError("boom")

    kinds = [e.type for e in fake_runner.events]
    assert kinds == [
        AgentEventType.MODEL_INTERACTION_STARTED,
        AgentEventType.MODEL_INTERACTION_ENDED,
    ]
    ended = fake_runner.events[-1]
    assert ended.usage is None and ended.model is None


def test_malformed_tool_arguments_degrade_to_empty_input(fake_runner: _FakeRunner):
    obs = h.model_call_observer_provider("gpt-5.1", fake_runner)
    assert obs is not None
    with obs:
        obs.on_response(_response([_fn_call("call_BAD", "lookup", "{not json")]))

    requested = [e for e in fake_runner.events if isinstance(e, ToolRequested)]
    assert len(requested) == 1 and requested[0].tool_input == {}


def test_missing_usage_still_closes_the_bracket(fake_runner: _FakeRunner):
    # `ModelResponse.usage` is typed non-optional, so the observer reads it defensively rather
    # than trusting the annotation — usage is best-effort accounting, not a reason to break a
    # turn. A response that reports none must still close its bracket.
    obs = h.model_call_observer_provider("gpt-5.1", fake_runner)
    assert obs is not None
    with obs:
        obs.on_response(SimpleNamespace(output=[], usage=None))

    ended = fake_runner.events[-1]
    assert isinstance(ended, ModelInteractionEnded) and ended.usage is None


def test_provider_declines_when_there_is_no_harness_turn():
    # Not a harness runner at all (e.g. Runner.run called without context=) -> unobserved,
    # rather than raising and breaking the model call.
    assert h.model_call_observer_provider("gpt-5.1", object()) is None
    assert h.model_call_observer_provider("gpt-5.1", None) is None


def test_provider_declines_between_turns(fake_runner: _FakeRunner):
    fake_runner.current_stream_context = None
    assert h.model_call_observer_provider("gpt-5.1", fake_runner) is None


def test_select_falls_back_to_a_null_observer():
    # No provider configured, or a provider that declines: the plugin's call site stays a plain
    # `with` and the model call runs exactly as it did before observers existed.
    unconfigured = select_model_call_observer(provider=None, model="m", run_context=None)
    declining = select_model_call_observer(
        provider=lambda _m, _c: None, model="m", run_context=None
    )
    for observer in (unconfigured, declining):
        assert isinstance(observer, NullModelCallObserver)
        with observer as entered:
            entered.on_response(_response([]))
