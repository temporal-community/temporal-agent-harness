# ABOUTME: Tests for the Monty eval scorers — that each one reads the agent's actual tool calls
# off the event stream and scores the right thing, including the failure modes they exist to
# catch (a double booking, an unrequested booking, a hallucinated confirmation code, a booking
# that ignored the user's correction).
#
# A scorer you cannot test is a scorer you cannot trust: a broken one silently invalidates every
# number it produces. These build synthetic AgentEvents directly, so they need no model, no API
# key, and no Temporal server — which is what lets them run in CI alongside everything else.
#
# Run with: uv run pytest tests/examples/monty/test_evaluators.py -v

from __future__ import annotations

from typing import Any

from temporal_agent_harness.evals import ScriptResult, TurnScript, TurnStep
from temporal_agent_harness.harness.agent_client import TurnResult
from temporal_agent_harness.harness.agent_protocol import (
    AgentEvent,
    AgentReply,
    AgentStreamItem,
    TokenUsage,
    ToolStartEvent,
)

from examples.monty.activities import generate_flights, make_booking_ref
from examples.monty.evals import evaluators as ev
from examples.monty.travel_models import FlightSearchRequest

ROUTE = {"origin": "SFO", "destination": "JFK", "date": "2026-07-01"}


# ---------------------------------------------------------------------------
# Synthetic runs
# ---------------------------------------------------------------------------


def _event(item: AgentStreamItem, turn_id: str = "t1") -> AgentEvent:
    return AgentEvent(
        event=item, agent_id="a1b2c3", turn_id=turn_id, turn_number=1, timestamp=0.0
    )


def _tool(name: str, request: dict[str, Any], tool_id: str = "x") -> AgentEvent:
    # Mirrors how the harness records an activity tool's model-facing input: one pydantic
    # request model bound to the parameter named ``request``.
    return _event(
        ToolStartEvent(tool_id=tool_id, tool_name=name, tool_input={"request": request})
    )


def _turn(events: list[AgentEvent], reply_text: str = "") -> TurnResult[Any]:
    all_events = [*events, _event(AgentReply(output={"text": reply_text}))]
    return TurnResult(
        turn_id="t1",
        turn_number=1,
        output={"text": reply_text},
        typed=None,
        error=None,
        events=tuple(all_events),
        usage=TokenUsage(),
        model_interactions=1,
        otel_trace_id="",
        labels={},
        accepted_offset=0,
        resume_offset=0,
    )


def _result(events: list[AgentEvent], reply_text: str = "") -> ScriptResult:
    return ScriptResult(
        session_workflow_id="wf-1", turns=[_turn(events, reply_text)]
    )


def _script(**expected: Any) -> TurnScript:
    return TurnScript(
        steps=[TurnStep.text("go")],
        workflow_type="MontyChatAgent",
        task_queue="q",
        expected=expected or None,
    )


def _flights() -> list[Any]:
    return generate_flights(FlightSearchRequest(**ROUTE))


def _cheapest() -> Any:
    return min(_flights(), key=lambda f: f.price_usd)


def _priciest() -> Any:
    return max(_flights(), key=lambda f: f.price_usd)


def _booking(flight_id: str, passenger: str = "Ada Lovelace") -> dict[str, Any]:
    return {"flight_id": flight_id, "passenger_name": passenger}


def _by_name(scores: list[Any], name: str) -> Any:
    return next(s for s in scores if s.name == name)


# ---------------------------------------------------------------------------
# The simulated world really is deterministic (the premise everything rests on)
# ---------------------------------------------------------------------------


def test_flight_generation_is_deterministic():
    # If this ever stops holding, "did it book the cheapest?" stops being decidable and the
    # whole dataset silently degrades to a vibe check.
    assert [f.model_dump() for f in _flights()] == [f.model_dump() for f in _flights()]
    assert len(_flights()) >= 2


# ---------------------------------------------------------------------------
# booked_the_cheapest_flight
# ---------------------------------------------------------------------------


def test_cheapest_passes_when_the_cheapest_was_booked():
    cheapest = _cheapest()
    result = _result(
        [
            _tool("search_flights", ROUTE),
            _tool("book_flight", _booking(cheapest.flight_id)),
        ]
    )
    score = _by_name(
        ev.booked_the_cheapest_flight(_script(books_flight=True), result),
        "booked_the_cheapest_flight",
    )
    assert score.is_pass


def test_cheapest_fails_and_reports_the_overpayment():
    priciest, cheapest = _priciest(), _cheapest()
    if priciest.flight_id == cheapest.flight_id:  # pragma: no cover - only if all prices tie
        return
    result = _result(
        [
            _tool("search_flights", ROUTE),
            _tool("book_flight", _booking(priciest.flight_id)),
        ]
    )
    score = _by_name(
        ev.booked_the_cheapest_flight(_script(books_flight=True), result),
        "booked_the_cheapest_flight",
    )
    assert not score.is_pass
    # A failing score has to say WHY in a way you can act on — a red cell alone is useless.
    assert cheapest.flight_id in score.comment
    assert score.metadata["overpaid_usd"] > 0


def test_cheapest_is_skipped_when_the_case_expects_no_booking():
    # An evaluator that does not apply must stay silent rather than emit a free pass.
    assert ev.booked_the_cheapest_flight(_script(books_flight=False), _result([])) == []


def test_cheapest_flags_a_flight_id_from_no_search():
    result = _result(
        [
            _tool("search_flights", ROUTE),
            _tool("book_flight", _booking("FL-SFOLAX-001")),
        ]
    )
    score = _by_name(
        ev.booked_the_cheapest_flight(_script(books_flight=True), result),
        "booked_the_cheapest_flight",
    )
    assert not score.is_pass
    assert "matches none of the agent's own searches" in score.comment


# ---------------------------------------------------------------------------
# booked_exactly_once — the double-booking catch
# ---------------------------------------------------------------------------


def test_double_booking_is_caught():
    cheapest = _cheapest()
    result = _result(
        [
            _tool("search_flights", ROUTE),
            _tool("book_flight", _booking(cheapest.flight_id), tool_id="a"),
            _tool("book_flight", _booking(cheapest.flight_id), tool_id="b"),
        ]
    )
    score = _by_name(
        ev.booked_exactly_once(_script(books_flight=True), result), "booked_exactly_once"
    )
    # The reply would look perfectly correct here; only the call count exposes it.
    assert not score.is_pass
    assert score.metadata["calls"] == 2


def test_single_booking_passes():
    result = _result(
        [_tool("search_flights", ROUTE), _tool("book_flight", _booking("FL-SFOJFK-001"))]
    )
    assert _by_name(
        ev.booked_exactly_once(_script(books_flight=True), result), "booked_exactly_once"
    ).is_pass


# ---------------------------------------------------------------------------
# did_not_act_unrequested
# ---------------------------------------------------------------------------


def test_unrequested_booking_is_caught():
    result = _result(
        [_tool("search_flights", ROUTE), _tool("book_flight", _booking("FL-SFOJFK-001"))]
    )
    score = _by_name(
        ev.did_not_act_unrequested(_script(books_flight=False), result),
        "did_not_act_unrequested",
    )
    assert not score.is_pass


def test_search_only_run_passes():
    result = _result([_tool("search_flights", ROUTE)])
    assert _by_name(
        ev.did_not_act_unrequested(_script(books_flight=False), result),
        "did_not_act_unrequested",
    ).is_pass


# ---------------------------------------------------------------------------
# searched_before_booking
# ---------------------------------------------------------------------------


def test_booking_before_searching_is_caught():
    result = _result(
        [_tool("book_flight", _booking("FL-SFOJFK-001")), _tool("search_flights", ROUTE)]
    )
    score = _by_name(
        ev.searched_before_booking(_script(books_flight=True), result),
        "searched_before_booking",
    )
    # Booking first means the flight id was invented.
    assert not score.is_pass
    assert "book_flight -> search_flights" in score.comment


# ---------------------------------------------------------------------------
# honored_the_latest_search — the multi-turn memory check
# ---------------------------------------------------------------------------


def test_correction_ignored_is_caught():
    result = _result(
        [
            _tool("search_flights", ROUTE),
            _tool("search_flights", {**ROUTE, "date": "2026-07-01"}),
        ]
    )
    score = _by_name(
        ev.honored_the_latest_search(_script(date="2026-07-02"), result),
        "honored_the_latest_search",
    )
    assert not score.is_pass


def test_correction_honored_passes():
    result = _result(
        [
            _tool("search_flights", ROUTE),
            _tool("search_flights", {**ROUTE, "date": "2026-07-02"}),
        ]
    )
    assert _by_name(
        ev.honored_the_latest_search(_script(date="2026-07-02"), result),
        "honored_the_latest_search",
    ).is_pass


# ---------------------------------------------------------------------------
# carried_passenger_name
# ---------------------------------------------------------------------------


def test_invented_passenger_name_is_caught():
    result = _result(
        [
            _tool("search_flights", ROUTE),
            _tool("book_flight", _booking("FL-SFOJFK-001", "John Doe")),
        ]
    )
    score = _by_name(
        ev.carried_passenger_name(_script(passenger_name="Alan Turing"), result),
        "carried_passenger_name",
    )
    assert not score.is_pass
    assert "John Doe" in score.comment


def test_passenger_name_match_is_case_insensitive():
    result = _result(
        [_tool("book_flight", _booking("FL-SFOJFK-001", "alan turing"))]
    )
    assert _by_name(
        ev.carried_passenger_name(_script(passenger_name="Alan Turing"), result),
        "carried_passenger_name",
    ).is_pass


# ---------------------------------------------------------------------------
# reply_cites_real_confirmation_code — the anti-hallucination check
# ---------------------------------------------------------------------------


def test_reply_quoting_the_real_code_passes():
    booking = _booking("FL-SFOJFK-001")
    code = make_booking_ref("AIR", booking["flight_id"], booking["passenger_name"])
    result = _result(
        [_tool("search_flights", ROUTE), _tool("book_flight", booking)],
        reply_text=f"All set — your confirmation code is {code}.",
    )
    assert _by_name(
        ev.reply_cites_real_confirmation_code(_script(books_flight=True), result),
        "reply_cites_real_confirmation_code",
    ).is_pass


def test_reply_quoting_a_plausible_but_wrong_code_is_caught():
    result = _result(
        [_tool("search_flights", ROUTE), _tool("book_flight", _booking("FL-SFOJFK-001"))],
        # Well-formed and completely made up. A "looks like a code" regex would pass this.
        reply_text="Your confirmation code is AIR-ABC123.",
    )
    score = _by_name(
        ev.reply_cites_real_confirmation_code(_script(books_flight=True), result),
        "reply_cites_real_confirmation_code",
    )
    assert not score.is_pass
    assert "AIR-ABC123" in score.comment


def test_reply_omitting_the_code_is_caught():
    result = _result(
        [_tool("search_flights", ROUTE), _tool("book_flight", _booking("FL-SFOJFK-001"))],
        reply_text="Done! You're all booked.",
    )
    assert not _by_name(
        ev.reply_cites_real_confirmation_code(_script(books_flight=True), result),
        "reply_cites_real_confirmation_code",
    ).is_pass


# ---------------------------------------------------------------------------
# The suite as a whole
# ---------------------------------------------------------------------------


async def test_default_suite_scores_a_clean_run():
    from temporal_agent_harness.evals import run_evaluators

    cheapest = _cheapest()
    booking = _booking(cheapest.flight_id)
    code = make_booking_ref("AIR", booking["flight_id"], booking["passenger_name"])
    script = _script(books_flight=True, passenger_name="Ada Lovelace")
    result = _result(
        [_tool("search_flights", ROUTE), _tool("book_flight", booking)],
        reply_text=f"Booked! Confirmation {code}.",
    )

    scores = await run_evaluators(ev.DEFAULT_EVALUATORS, script, result)
    assert scores, "a clean run should still produce scores"
    assert all(s.is_pass for s in scores), [
        (s.name, s.comment) for s in scores if not s.is_pass
    ]


async def test_a_raising_evaluator_costs_one_score_not_the_run():
    from temporal_agent_harness.evals import run_evaluators

    def broken(script: TurnScript, result: ScriptResult) -> list[Any]:
        raise ValueError("scorer bug")

    scores = await run_evaluators(
        [broken, ev.booked_exactly_once], _script(books_flight=False), _result([])
    )
    # The bug is visible as a failing score, and the other evaluator still ran.
    assert not _by_name(scores, "broken").is_pass
    assert "scorer bug" in _by_name(scores, "broken").comment
    assert _by_name(scores, "booked_exactly_once").is_pass
