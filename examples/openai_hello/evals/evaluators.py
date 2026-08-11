"""Scorers for the OpenAI Hello dataset.

Same shape as the Monty scorers — plain functions of ``(script, result)`` reading the harness's
event stream — and that is the interesting part: nothing here knows or cares that the agent is
driven by the OpenAI Agents SDK rather than Gemini. Tool calls land on the same stream with the
same event types either way, so a scorer written for one agent's tools ports to another by
changing the tool name.

The agent's one tool returns a canned, deterministic string, so "did it report the temperature
the tool actually gave it?" is decidable — the same trick that makes the Monty scorers exact.
"""

from __future__ import annotations

import re

from temporal_agent_harness.evals import Score, ScriptResult, TurnScript
from temporal_agent_harness.harness.agent_protocol import AgentEventType

WEATHER_TOOL = "get_weather"

# The canned reply is "It's 72°F and sunny in {city}." — so the number the agent is entitled to
# state is whatever appears in the tool's own output.
_TEMPERATURE_RE = re.compile(r"(\d{1,3})\s*°?\s*F", re.IGNORECASE)


def _weather_calls(result: ScriptResult) -> list[str]:
    """The ``city`` argument of every get_weather call, in order across all turns."""
    return [
        str(event.tool_input.get("city", ""))
        for event in result.events_of_type(AgentEventType.TOOL_START)
        if event.tool_name == WEATHER_TOOL
    ]


def _norm(cities: list[str]) -> list[str]:
    return [c.strip().lower() for c in cities if c.strip()]


def called_the_tool_for_the_right_cities(
    script: TurnScript, result: ScriptResult
) -> list[Score]:
    """Did the agent look up exactly the cities the case expects — no more, no fewer?

    One evaluator covers both directions on purpose. Splitting "looked up what it should" from
    "looked up nothing it shouldn't" would double-report a single mistake, and the comparison
    that matters is the whole set.
    """
    expected = (script.expected or {}).get("weather_cities")
    if expected is None:
        return []
    want, got = _norm(list(expected)), _norm(_weather_calls(result))
    if want == got:
        return [
            Score.passed(
                "called_the_tool_for_the_right_cities",
                f"looked up {got or 'nothing'}, as expected",
            )
        ]
    return [
        Score.failed(
            "called_the_tool_for_the_right_cities",
            f"looked up {got or 'nothing'}, expected {want or 'nothing'}",
            missing=[c for c in want if c not in got],
            unexpected=[c for c in got if c not in want],
        )
    ]


def did_not_use_a_tool_it_did_not_need(
    script: TurnScript, result: ScriptResult
) -> list[Score]:
    """On a case expecting no lookup, did the agent stay its hand?

    Scored separately from correctness because it is the failure people actually ship: a
    one-tool agent that calls its tool at everything looks fine in a demo and wastes a round
    trip on every unrelated message.
    """
    expected = (script.expected or {}).get("weather_cities")
    if expected is None or expected:
        return []
    called = _weather_calls(result)
    return [
        Score.boolean(
            "did_not_use_a_tool_it_did_not_need",
            not called,
            comment=(
                "no tool call, as expected"
                if not called
                else f"called {WEATHER_TOOL} for {called} on a question that needed no lookup"
            ),
        )
    ]


def reply_states_the_temperature_the_tool_returned(
    script: TurnScript, result: ScriptResult
) -> list[Score]:
    """Anti-hallucination: is the number in the reply the number the tool gave?

    The tool's output is canned and deterministic, so the only defensible temperature is the one
    it returned. A reply that states a *different* number is caught — which a "does it mention a
    temperature?" check would happily pass.
    """
    if not (script.expected or {}).get("weather_cities"):
        return []

    tool_outputs = [
        event.tool_output
        for event in result.events_of_type(AgentEventType.TOOL_END)
        if event.tool_name == WEATHER_TOOL
    ]
    if not tool_outputs:
        return [
            Score.failed(
                "reply_states_the_temperature_the_tool_returned",
                "the tool never ran, so the reply cannot be grounded in it",
            )
        ]

    allowed = {m for output in tool_outputs for m in _TEMPERATURE_RE.findall(output)}
    stated = set(_TEMPERATURE_RE.findall(result.final_text))
    if not stated:
        return [
            Score.failed(
                "reply_states_the_temperature_the_tool_returned",
                f"reply states no temperature; the tool returned {sorted(allowed)}",
            )
        ]
    invented = stated - allowed
    return [
        Score.boolean(
            "reply_states_the_temperature_the_tool_returned",
            not invented,
            comment=(
                f"reply states {sorted(stated)}, tool returned {sorted(allowed)}"
                if not invented
                else f"reply states {sorted(invented)}, which the tool never returned "
                f"(it returned {sorted(allowed)})"
            ),
        )
    ]


def did_not_look_up_the_same_city_twice(
    script: TurnScript, result: ScriptResult
) -> list[Score]:
    """Did the agent re-fetch something it already had?

    A follow-up question about an answer already given should be answered from context. Calling
    the tool again is not wrong output — which is exactly why only the call stream reveals it.
    """
    called = _norm(_weather_calls(result))
    duplicates = {c for c in called if called.count(c) > 1}
    return [
        Score.boolean(
            "did_not_look_up_the_same_city_twice",
            not duplicates,
            comment=(
                f"tool calls: {called or 'none'}"
                if not duplicates
                else f"looked up {sorted(duplicates)} more than once: {called}"
            ),
        )
    ]


#: The default suite. Order is presentation only — every evaluator is independent.
DEFAULT_EVALUATORS = [
    called_the_tool_for_the_right_cities,
    did_not_use_a_tool_it_did_not_need,
    reply_states_the_temperature_the_tool_returned,
    did_not_look_up_the_same_city_twice,
]
