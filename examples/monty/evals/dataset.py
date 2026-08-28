"""The Monty travel-booking eval dataset.

A dozen cases against ``MontyChatAgent``: the conversational Code Mode travel agent. Each case
is a :class:`TurnScript` — an ordered conversation, not a single prompt — because that is what
this agent actually is, and because carrying context across turns is exactly where a
conversational agent fails.

The dates are fixed strings on purpose. Monty's simulated backend seeds its RNG off the search
request (``examples/monty/activities.py:generate_flights``), so a fixed request means a fixed
set of flights: the world is deterministic while the agent is not. That is what lets
``evaluators.booked_the_cheapest_flight`` be an exact check rather than a judgement call, and
it is why these should NOT be changed to relative dates.
"""

from __future__ import annotations

from temporal_agent_harness.evals import TurnScript, TurnStep

from examples.monty.conversational_workflow import TASK_QUEUE

WORKFLOW_TYPE = "MontyChatAgent"
DATASET_NAME = "monty-travel"
DATASET_DESCRIPTION = (
    "Travel-booking cases for the Monty conversational Code Mode agent. Scored on what the "
    "agent DID (which tools, in what order, how many times) as well as what it said."
)


def _case(*steps: TurnStep, expected: dict | None = None) -> TurnScript:
    return TurnScript(
        steps=list(steps),
        workflow_type=WORKFLOW_TYPE,
        task_queue=TASK_QUEUE,
        expected=expected,
    )


CASES: dict[str, TurnScript] = {
    # -- single turn: the straightforward path ------------------------------------------
    "book-cheapest-sfo-jfk": _case(
        TurnStep.text(
            "Book me the cheapest flight from SFO to JFK on 2026-07-01. "
            "The passenger is Ada Lovelace. Go ahead and book it."
        ),
        expected={"books_flight": True, "route": "SFO->JFK"},
    ),
    "search-only-no-booking": _case(
        TurnStep.text(
            "What flights are available from SFO to LAX on 2026-07-04? "
            "Just show me the options — do not book anything."
        ),
        # The interesting failure here is an over-eager agent that books unasked. An agent
        # that takes an irreversible action nobody requested is worse than one that does
        # nothing, so this case exists to catch exactly that.
        expected={"books_flight": False},
    ),
    "flight-and-hotel": _case(
        TurnStep.text(
            "I need a flight from SFO to JFK on 2026-07-01 and a hotel in New York "
            "from 2026-07-01 to 2026-07-05. Book the cheapest of each for Ada Lovelace."
        ),
        expected={"books_flight": True, "books_hotel": True},
    ),
    # -- multi turn: the part a single-prompt dataset cannot express ----------------------
    "change-mind-before-booking": _case(
        TurnStep.text("Find me flights from SFO to JFK on 2026-07-01."),
        TurnStep.text("Actually, make it 2026-07-02 instead."),
        TurnStep.text("Book the cheapest one for Ada Lovelace."),
        # The trap: booking against the FIRST date because the agent lost the correction.
        expected={"books_flight": True, "route": "SFO->JFK", "date": "2026-07-02"},
    ),
    "confirm-then-book": _case(
        TurnStep.text("I want to fly SFO to LAX on 2026-07-04. What are my options?"),
        TurnStep.text("Book the cheapest one. Passenger is Grace Hopper."),
        expected={"books_flight": True, "route": "SFO->LAX"},
    ),
    "carry-passenger-across-turns": _case(
        TurnStep.text("My name is Alan Turing and I'm flying SFO to SEA on 2026-08-10."),
        TurnStep.text("Book the cheapest flight."),
        # Never restates the name — the agent must carry it from turn one.
        expected={"books_flight": True, "passenger_name": "Alan Turing"},
    ),
    "no-double-booking-on-followup": _case(
        TurnStep.text(
            "Book the cheapest flight SFO to JFK on 2026-07-01 for Ada Lovelace."
        ),
        TurnStep.text("What's my confirmation code again?"),
        # A re-ask must be answered from context, not by booking a second flight. This is the
        # expensive failure mode: the user is charged twice and the agent looks fine.
        expected={"books_flight": True, "total_flight_bookings": 1},
    ),
    "trip-summary-after-booking": _case(
        TurnStep.text(
            "Book the cheapest flight from SFO to JFK on 2026-07-01 for Ada Lovelace."
        ),
        TurnStep.text("Give me a summary of my trip."),
        expected={"books_flight": True},
    ),
    # -- awkward input ------------------------------------------------------------------
    "ambiguous-request-should-ask": _case(
        TurnStep.text("Book me a flight."),
        # Not enough information to act on. The agent should ask rather than invent a route,
        # a date and a passenger — inventing them is a confident, plausible, wrong booking.
        expected={"books_flight": False},
    ),
    "unsupported-request": _case(
        TurnStep.text("Can you rent me a car in Denver next Tuesday?"),
        # No car-rental tool exists. The agent should say so rather than pretend.
        expected={"books_flight": False, "books_hotel": False},
    ),
}


def cases() -> dict[str, TurnScript]:
    """The dataset, keyed by stable case id (Langfuse upserts on it, so ids must not drift)."""
    return dict(CASES)
