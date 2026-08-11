# ABOUTME: Tests for the OpenAI Hello eval scorers.
#
# The cross-SDK point, made concretely: these are built from the same synthetic AgentEvents as
# the Monty scorer tests, because tool calls land on the same stream with the same event types
# whichever AI SDK drove them. Nothing here imports anything OpenAI-specific.
#
# No model, no API key, no Temporal server.
#
# Run with: uv run pytest tests/examples/openai_hello/test_evaluators.py -v

from __future__ import annotations

from typing import Any

from temporal_agent_harness.evals import ScriptResult, TurnScript, TurnStep
from temporal_agent_harness.harness.agent_client import TurnResult
from temporal_agent_harness.harness.agent_protocol import (
    AgentEvent,
    AgentReply,
    AgentStreamItem,
    TokenUsage,
    ToolEndEvent,
    ToolStartEvent,
)

from examples.openai_hello.evals import evaluators as ev

CANNED = "It's 72°F and sunny in {city}."


def _event(item: AgentStreamItem, turn_id: str = "t1") -> AgentEvent:
    return AgentEvent(
        event=item, agent_id="a1b2c3", turn_id=turn_id, turn_number=1, timestamp=0.0
    )


def _lookup(city: str, tool_id: str | None = None) -> list[AgentEvent]:
    """A complete get_weather call: start (with args) + end (with the canned output)."""
    tool_id = tool_id or f"w-{city}"
    return [
        _event(
            ToolStartEvent(
                tool_id=tool_id, tool_name="get_weather", tool_input={"city": city}
            )
        ),
        _event(
            ToolEndEvent(
                tool_id=tool_id,
                tool_name="get_weather",
                tool_output=CANNED.format(city=city),
            )
        ),
    ]


def _turn(events: list[AgentEvent], reply: str) -> TurnResult[Any]:
    return TurnResult(
        turn_id="t1",
        turn_number=1,
        output={"text": reply},
        typed=None,
        error=None,
        events=tuple([*events, _event(AgentReply(output={"text": reply}))]),
        usage=TokenUsage(),
        model_interactions=1,
        otel_trace_id="",
        labels={},
        accepted_offset=0,
        resume_offset=0,
    )


def _result(events: list[AgentEvent], reply: str = "") -> ScriptResult:
    return ScriptResult(session_workflow_id="wf-1", turns=[_turn(events, reply)])


def _script(**expected: Any) -> TurnScript:
    return TurnScript(
        steps=[TurnStep.text("go")],
        workflow_type="OpenAIHelloAgent",
        task_queue="openai-hello",
        expected=expected or None,
    )


def _by_name(scores: list[Any], name: str) -> Any:
    return next(s for s in scores if s.name == name)


# ---------------------------------------------------------------------------
# called_the_tool_for_the_right_cities
# ---------------------------------------------------------------------------


def test_right_city_passes():
    result = _result(_lookup("Tokyo"), "It's 72°F and sunny in Tokyo.")
    assert _by_name(
        ev.called_the_tool_for_the_right_cities(
            _script(weather_cities=["Tokyo"]), result
        ),
        "called_the_tool_for_the_right_cities",
    ).is_pass


def test_missing_lookup_is_caught_and_named():
    # Asked to compare two cities, only looked up one — and inferred the other.
    result = _result(_lookup("Paris"), "Paris is 72°F; Berlin is probably similar.")
    score = _by_name(
        ev.called_the_tool_for_the_right_cities(
            _script(weather_cities=["Paris", "Berlin"]), result
        ),
        "called_the_tool_for_the_right_cities",
    )
    assert not score.is_pass
    assert score.metadata["missing"] == ["berlin"]


def test_unexpected_lookup_is_caught():
    result = _result(_lookup("Tokyo"), "I'm a weather assistant.")
    score = _by_name(
        ev.called_the_tool_for_the_right_cities(_script(weather_cities=[]), result),
        "called_the_tool_for_the_right_cities",
    )
    assert not score.is_pass
    assert score.metadata["unexpected"] == ["tokyo"]


def test_city_matching_is_case_insensitive():
    result = _result(_lookup("tokyo"), "72°F")
    assert _by_name(
        ev.called_the_tool_for_the_right_cities(
            _script(weather_cities=["Tokyo"]), result
        ),
        "called_the_tool_for_the_right_cities",
    ).is_pass


# ---------------------------------------------------------------------------
# did_not_use_a_tool_it_did_not_need
# ---------------------------------------------------------------------------


def test_over_eager_tool_use_is_caught():
    # The classic one-tool-agent failure: reaches for its tool at an unrelated message.
    result = _result(_lookup("Tokyo"), "Hello!")
    assert not _by_name(
        ev.did_not_use_a_tool_it_did_not_need(_script(weather_cities=[]), result),
        "did_not_use_a_tool_it_did_not_need",
    ).is_pass


def test_answering_directly_passes():
    result = _result([], "I'm a friendly weather assistant.")
    assert _by_name(
        ev.did_not_use_a_tool_it_did_not_need(_script(weather_cities=[]), result),
        "did_not_use_a_tool_it_did_not_need",
    ).is_pass


def test_evaluator_is_silent_when_a_lookup_was_expected():
    # Must not emit a passing score on a case it does not apply to.
    assert (
        ev.did_not_use_a_tool_it_did_not_need(
            _script(weather_cities=["Tokyo"]), _result(_lookup("Tokyo"))
        )
        == []
    )


# ---------------------------------------------------------------------------
# reply_states_the_temperature_the_tool_returned
# ---------------------------------------------------------------------------


def test_reply_quoting_the_tool_temperature_passes():
    result = _result(_lookup("Tokyo"), "It's currently 72°F and sunny in Tokyo.")
    assert _by_name(
        ev.reply_states_the_temperature_the_tool_returned(
            _script(weather_cities=["Tokyo"]), result
        ),
        "reply_states_the_temperature_the_tool_returned",
    ).is_pass


def test_invented_temperature_is_caught():
    # The tool said 72; the reply says 68. A "mentions a temperature?" check passes this.
    result = _result(_lookup("Tokyo"), "It's about 68°F in Tokyo right now.")
    score = _by_name(
        ev.reply_states_the_temperature_the_tool_returned(
            _script(weather_cities=["Tokyo"]), result
        ),
        "reply_states_the_temperature_the_tool_returned",
    )
    assert not score.is_pass
    assert "68" in score.comment


def test_reply_with_no_temperature_is_caught():
    result = _result(_lookup("Tokyo"), "It's nice out in Tokyo.")
    assert not _by_name(
        ev.reply_states_the_temperature_the_tool_returned(
            _script(weather_cities=["Tokyo"]), result
        ),
        "reply_states_the_temperature_the_tool_returned",
    ).is_pass


def test_temperature_check_needs_the_tool_to_have_run():
    result = _result([], "It's 72°F in Tokyo.")
    score = _by_name(
        ev.reply_states_the_temperature_the_tool_returned(
            _script(weather_cities=["Tokyo"]), result
        ),
        "reply_states_the_temperature_the_tool_returned",
    )
    # Stating the right number without calling the tool is a guess that happened to be right.
    assert not score.is_pass
    assert "never ran" in score.comment


# ---------------------------------------------------------------------------
# did_not_look_up_the_same_city_twice
# ---------------------------------------------------------------------------


def test_refetching_the_same_city_is_caught():
    result = _result(
        [*_lookup("Tokyo", "a"), *_lookup("Tokyo", "b")], "Still 72°F in Tokyo."
    )
    score = _by_name(
        ev.did_not_look_up_the_same_city_twice(_script(weather_cities=["Tokyo"]), result),
        "did_not_look_up_the_same_city_twice",
    )
    # The output is fine; only the call stream shows the wasted round trip.
    assert not score.is_pass
    assert "tokyo" in score.comment


def test_two_different_cities_are_not_duplicates():
    result = _result([*_lookup("Tokyo"), *_lookup("Paris")], "Both are 72°F.")
    assert _by_name(
        ev.did_not_look_up_the_same_city_twice(
            _script(weather_cities=["Tokyo", "Paris"]), result
        ),
        "did_not_look_up_the_same_city_twice",
    ).is_pass


# ---------------------------------------------------------------------------
# The suite
# ---------------------------------------------------------------------------


async def test_default_suite_scores_a_clean_run():
    from temporal_agent_harness.evals import run_evaluators

    result = _result(_lookup("Tokyo"), "It's 72°F and sunny in Tokyo.")
    scores = await run_evaluators(
        ev.DEFAULT_EVALUATORS, _script(weather_cities=["Tokyo"]), result
    )
    assert scores
    assert all(s.is_pass for s in scores), [
        (s.name, s.comment) for s in scores if not s.is_pass
    ]
