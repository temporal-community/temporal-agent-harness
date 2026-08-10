"""Scorers for the Monty travel dataset.

These are *process* evaluators: they judge what the agent DID, not only what it said. That is
possible because every tool call the agent made is on the harness's event stream with its input
recorded, and because Monty's simulated backend is deterministic — given the search the agent
performed, ``generate_flights`` reproduces exactly the options it was looking at. So "did it
book the cheapest available flight?" is decidable, not a matter of opinion.

Each is a plain function of ``(script, result)`` returning ``list[Score]`` — no Langfuse import,
no network, no model. That means they are unit-testable against synthetic events (see
``tests/examples/monty/test_evaluators.py``), which matters: a scorer you cannot test is a
scorer you cannot trust, and a broken scorer silently invalidates every number it produces.

A model-graded evaluator (LLM-as-judge) lives in ``judge.py`` and plugs in the same way.
"""

from __future__ import annotations

import re
from typing import Any

from temporal_agent_harness.evals import Score, ScriptResult, TurnScript
from temporal_agent_harness.harness.agent_protocol import AgentEventType

from examples.monty.activities import generate_flights, make_booking_ref
from examples.monty.travel_models import FlightSearchRequest

# Confirmation codes are ``AIR-XXXXXX`` / ``HTL-XXXXXX`` (see ``make_booking_ref``).
_CONFIRMATION_RE = re.compile(r"\b(?:AIR|HTL)-[0-9A-F]{6}\b")


# ---------------------------------------------------------------------------
# Reading the agent's actions off the event stream
# ---------------------------------------------------------------------------


def _tool_calls(result: ScriptResult, tool_name: str) -> list[dict[str, Any]]:
    """The model-facing input of every call to ``tool_name``, in order across all turns."""
    return [
        _unwrap(event.tool_input)
        for event in result.events_of_type(AgentEventType.TOOL_START)
        if event.tool_name == tool_name
    ]


def _unwrap(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a single-``request``-parameter tool input to the request body itself.

    Monty's tools take one pydantic request model, so the recorded input is
    ``{"request": {...}}``. Tolerates the unwrapped shape too, since Code Mode host calls and
    direct tool calls can differ in how the argument is bound.
    """
    inner = tool_input.get("request")
    return inner if isinstance(inner, dict) else tool_input


def _tool_sequence(result: ScriptResult) -> list[str]:
    """Every tool name the agent invoked, in order — the shape of what it did."""
    return [e.tool_name for e in result.events_of_type(AgentEventType.TOOL_START)]


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------


def booked_the_cheapest_flight(script: TurnScript, result: ScriptResult) -> list[Score]:
    """Of the flights its own search returned, did the agent book the cheapest?

    Ground truth comes from replaying the agent's OWN search request through the deterministic
    generator, so this compares the booking against exactly the options the agent had in front
    of it — not against some independently-imagined ideal.

    Skipped (no score) when the case did not ask for a booking; an evaluator that does not apply
    should stay silent rather than emit a passing score it did not actually check.
    """
    if not (script.expected or {}).get("books_flight"):
        return []

    bookings = _tool_calls(result, "book_flight")
    searches = _tool_calls(result, "search_flights")
    if not bookings:
        return [Score.failed("booked_the_cheapest_flight", "no flight was booked")]
    if not searches:
        return [
            Score.failed(
                "booked_the_cheapest_flight", "booked without ever searching for flights"
            )
        ]

    booked_id = bookings[-1].get("flight_id", "")
    # Match the booking to the LAST search on the same route. The flight id encodes the route
    # but NOT the date (``FL-SFOJFK-001`` is ambiguous across dates), so on a case where the
    # user changed the date this deliberately scores against the most recent search — and
    # ``honored_the_latest_search`` is the evaluator that checks the date separately.
    candidates: list[Any] = []
    matched: dict[str, Any] | None = None
    for search in reversed(searches):
        try:
            request = FlightSearchRequest.model_validate(search)
        except Exception:  # noqa: BLE001 — a malformed search is its own failure, scored below
            continue
        if booked_id.startswith(f"FL-{request.origin}{request.destination}-"):
            matched, candidates = search, generate_flights(request)
            break

    if matched is None or not candidates:
        return [
            Score.failed(
                "booked_the_cheapest_flight",
                f"booked {booked_id!r}, which matches none of the agent's own searches "
                f"({[s.get('origin', '') + '->' + s.get('destination', '') for s in searches]})",
            )
        ]

    cheapest = min(candidates, key=lambda f: f.price_usd)
    booked = next((f for f in candidates if f.flight_id == booked_id), None)
    if booked is None:
        return [
            Score.failed(
                "booked_the_cheapest_flight",
                f"booked {booked_id!r}, which was not among the search results",
            )
        ]
    if booked.flight_id == cheapest.flight_id:
        return [
            Score.passed(
                "booked_the_cheapest_flight",
                f"booked {booked.flight_id} at ${booked.price_usd}",
            )
        ]
    return [
        Score.failed(
            "booked_the_cheapest_flight",
            f"booked {booked.flight_id} at ${booked.price_usd} when "
            f"{cheapest.flight_id} at ${cheapest.price_usd} was available",
            overpaid_usd=round(booked.price_usd - cheapest.price_usd, 2),
        )
    ]


def booked_exactly_once(script: TurnScript, result: ScriptResult) -> list[Score]:
    """Did the agent make exactly as many bookings as the case called for?

    The expensive failure mode: a follow-up question ("what's my code again?") answered by
    booking a second flight. The user is charged twice and the reply still looks correct, so
    nothing but a call count catches it.
    """
    expected = script.expected or {}
    wants_flight = bool(expected.get("books_flight"))
    want = int(expected.get("total_flight_bookings", 1 if wants_flight else 0))
    got = len(_tool_calls(result, "book_flight"))
    return [
        Score.boolean(
            "booked_exactly_once",
            got == want,
            comment=f"book_flight called {got}x, expected {want}x",
            calls=got,
        )
    ]


def did_not_act_unrequested(script: TurnScript, result: ScriptResult) -> list[Score]:
    """On a look-don't-touch case, did the agent refrain from booking anything?

    An agent that takes an irreversible action nobody asked for is worse than one that does
    nothing — which is why this is scored separately from correctness rather than folded into it.
    """
    expected = script.expected or {}
    if expected.get("books_flight") or expected.get("books_hotel"):
        return []
    booked = _tool_calls(result, "book_flight") + _tool_calls(result, "book_hotel")
    return [
        Score.boolean(
            "did_not_act_unrequested",
            not booked,
            comment=(
                "no booking made, as expected"
                if not booked
                else f"booked {len(booked)} unrequested item(s): {_tool_sequence(result)}"
            ),
        )
    ]


def searched_before_booking(script: TurnScript, result: ScriptResult) -> list[Score]:
    """Did every booking follow a search? Ordering is a correctness property here.

    Booking before searching means the agent invented a flight id, which produces a confident,
    plausible, wrong booking — the hardest kind of failure to notice from the reply text alone.
    """
    sequence = _tool_sequence(result)
    if "book_flight" not in sequence:
        return []
    first_search = sequence.index("search_flights") if "search_flights" in sequence else None
    first_book = sequence.index("book_flight")
    ok = first_search is not None and first_search < first_book
    return [
        Score.boolean(
            "searched_before_booking",
            ok,
            comment=f"tool order: {' -> '.join(sequence)}",
        )
    ]


def honored_the_latest_search(script: TurnScript, result: ScriptResult) -> list[Score]:
    """When the case expects a specific date, did the agent's final search actually use it?

    This is the multi-turn memory check: the user corrects the date mid-conversation, and the
    agent must carry the correction into the search it books against. Checked on the search
    rather than the booking because the flight id does not encode the date.
    """
    want_date = (script.expected or {}).get("date")
    if not want_date:
        return []
    searches = _tool_calls(result, "search_flights")
    if not searches:
        return [Score.failed("honored_the_latest_search", "no search was performed")]
    used = searches[-1].get("date")
    return [
        Score.boolean(
            "honored_the_latest_search",
            used == want_date,
            comment=f"last search used date {used!r}, expected {want_date!r}",
        )
    ]


def carried_passenger_name(script: TurnScript, result: ScriptResult) -> list[Score]:
    """When the name was given in an earlier turn, did the booking use it?

    Fails loudly on an invented name — an agent that books for "John Doe" rather than asking is
    doing something worse than failing.
    """
    want_name = (script.expected or {}).get("passenger_name")
    if not want_name:
        return []
    bookings = _tool_calls(result, "book_flight")
    if not bookings:
        return [Score.failed("carried_passenger_name", "no flight was booked")]
    used = bookings[-1].get("passenger_name", "")
    return [
        Score.boolean(
            "carried_passenger_name",
            used.strip().lower() == str(want_name).strip().lower(),
            comment=f"booked for {used!r}, expected {want_name!r}",
        )
    ]


def reply_cites_real_confirmation_code(
    script: TurnScript, result: ScriptResult
) -> list[Score]:
    """Does the final reply quote the confirmation code the booking actually produced?

    The anti-hallucination check. The code is a deterministic function of the booking input
    (``make_booking_ref``), so the correct answer is computable — and a reply that states a
    *different* well-formed code is caught, which a mere "does it look like a code?" regex would
    wave through.
    """
    if not (script.expected or {}).get("books_flight"):
        return []
    bookings = _tool_calls(result, "book_flight")
    if not bookings:
        return [Score.failed("reply_cites_real_confirmation_code", "no flight was booked")]

    booking = bookings[-1]
    expected_code = make_booking_ref(
        "AIR", booking.get("flight_id", ""), booking.get("passenger_name", "")
    )
    reply = result.final_text
    if expected_code in reply:
        return [Score.passed("reply_cites_real_confirmation_code", expected_code)]

    stated = _CONFIRMATION_RE.findall(reply)
    if stated:
        return [
            Score.failed(
                "reply_cites_real_confirmation_code",
                f"reply states {stated} but the booking produced {expected_code}",
            )
        ]
    return [
        Score.failed(
            "reply_cites_real_confirmation_code",
            f"reply never mentions the confirmation code {expected_code}",
        )
    ]


#: The default suite. Order is presentation only — every evaluator is independent.
DEFAULT_EVALUATORS = [
    booked_the_cheapest_flight,
    booked_exactly_once,
    did_not_act_unrequested,
    searched_before_booking,
    honored_the_latest_search,
    carried_passenger_name,
    reply_cites_real_confirmation_code,
]
