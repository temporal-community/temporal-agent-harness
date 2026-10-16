"""The agent's running TODO board of trips, as OBSERVABLE state.

The travel tools in ``activities.py`` do the work; this module is the agent's memory of
what it is collecting — one entry per trip, with the description, the flights and hotels
booked so far, and what is still left to do.

It is deliberately *not* an activity. The board is the agent's own notes, not an action
on the outside world, so every tool here is an ``@agent.tool_defn`` that runs INLINE in
the workflow and writes to a :class:`~temporal_agent_harness.harness.state.StateRef` the
workflow owns. That is what makes it observable: the workflow declares it once, as the class
attribute ``trip_board = agent.state(TripBoard)``, and from then on the harness publishes a
snapshot and then one RFC 6902 patch per committed ``mutate()`` block onto the agent's
``turn_events`` stream. The console's AGENT STATE pane folds those back into the document
and marks exactly what each commit touched — so a booking landing on the board shows up as
``add /trips/0/flights/-`` next to the ``tool_end`` that produced it, in order.

Compare ``examples/callback_tools/coding_agent/tools.py``, whose ``todowrite`` writes to a
plain ``Injected[list]``: same idea, same inline-tool shape, but nothing outside the
workflow can see it change. The only difference here is what the sink is.

Each tool takes ONE request object and returns ONE response object, like the travel tools,
so every host function a script calls has the same shape. The ``board`` parameter is
``Injected``: the workflow supplies it per call and it is hidden from the model and from
the generated Code Mode stubs, so a script never passes it and cannot address it.

NB: no ``from __future__ import annotations`` — these annotations are read concretely to
build the model-facing tool schemas and the Code Mode type-check stubs, and they cross
Temporal's pydantic converter.
"""

from typing import Literal

from pydantic import BaseModel
from temporalio import workflow

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.state import HarnessState, StateRef


TripStatus = Literal["collecting", "booked", "cancelled"]
BookingKind = Literal["flight", "hotel"]

# What a trip starts out owing when the caller does not say. Seeded rather than left empty
# so the board is useful the moment a trip is opened, and so "what's left" is a real list
# the agent ticks down rather than something it has to invent first.
DEFAULT_TASKS = (
    "Confirm dates and traveler",
    "Search and book the outbound flight",
    "Search and book the return flight",
    "Search and book the hotel",
    "Send the itinerary",
)


# ---------------------------------------------------------------------------
# The observable state
# ---------------------------------------------------------------------------
#
# Plain HarnessState models: immutable at rest, mutable only inside `ref.mutate()`.
# Every field below is addressable by JSON pointer, which is the whole point — ticking one
# task emits `replace /trips/0/remaining/2/done`, not a re-send of the trip.


class TripTask(HarnessState):
    """One thing the trip still needs, and whether it has been done."""

    text: str
    done: bool = False


class Booking(HarnessState):
    """A flight or hotel this trip has actually booked."""

    kind: BookingKind
    confirmation_code: str
    """The code a `book_flight` / `book_hotel` host call returned. Never invented."""
    detail: str
    """Human-readable: airline, times, hotel name, nights — whatever the reader needs."""
    price_usd: float = 0.0


class Trip(HarnessState):
    """One trip the agent is collecting."""

    trip_id: str
    description: str
    traveler: str
    status: TripStatus = "collecting"
    flights: list[Booking] = []
    hotels: list[Booking] = []
    remaining: list[TripTask] = []


class TripBoard(HarnessState):
    """Every trip this session is working on, newest last."""

    trips: list[Trip] = []


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class OpenTripRequest(BaseModel):
    description: str
    traveler: str
    tasks: list[str] = []
    """The checklist to start with. Empty means the standard one."""


class TripTasksRequest(BaseModel):
    trip_id: str
    tasks: list[str]


class RecordBookingRequest(BaseModel):
    trip_id: str
    kind: BookingKind
    confirmation_code: str
    detail: str
    price_usd: float = 0.0
    completes: list[str] = []
    """Checklist items this booking finishes, ticked off in the same commit."""


class TripStatusRequest(BaseModel):
    trip_id: str
    status: TripStatus


class TripBoardResponse(BaseModel):
    trip_id: str
    status: str
    remaining: list[str]
    note: str
    """What the call did — including anything it was asked for and could not find."""
    board: str
    """The whole board, rendered, so the caller always sees the result of its own write."""


# ---------------------------------------------------------------------------
# Helpers (pure; no state access)
# ---------------------------------------------------------------------------


def _render(board: TripBoard) -> str:
    if not board.trips:
        return "(no trips on the board yet)"
    lines: list[str] = []
    for trip in board.trips:
        lines.append(f"[{trip.trip_id}] {trip.description} — {trip.traveler} ({trip.status})")
        for booking in [*trip.flights, *trip.hotels]:
            price = f" ${booking.price_usd:,.2f}" if booking.price_usd else ""
            lines.append(
                f"    {booking.kind}: {booking.detail} — {booking.confirmation_code}{price}"
            )
        left = [task.text for task in trip.remaining if not task.done]
        lines.append(f"    remaining: {'; '.join(left) if left else '(nothing — trip is complete)'}")
    return "\n".join(lines)


def _find_trip(board: TripBoard, trip_id: str) -> int | None:
    """The index of ``trip_id``, or of the newest trip when it is blank or ``latest``.

    The convenience matters: a script that just opened a trip has the id, but a model
    resuming a conversation three turns later often does not, and the alternative is that
    it guesses one — which silently writes nothing.
    """
    wanted = trip_id.strip()
    if not wanted or wanted.lower() == "latest":
        return len(board.trips) - 1 if board.trips else None
    for index, trip in enumerate(board.trips):
        if trip.trip_id == wanted:
            return index
    return None


def _match_task(tasks: list[TripTask], wanted: str) -> int | None:
    """The open task ``wanted`` names: exact text first, then a unique-enough substring.

    Substring as a fallback because the caller is a model writing prose ("the hotel") for
    a task the board spells out in full ("Search and book the hotel"), and refusing that
    leaves the checklist permanently stale for no good reason.
    """
    lowered = wanted.strip().lower()
    if not lowered:
        return None
    for index, task in enumerate(tasks):
        if not task.done and task.text.lower() == lowered:
            return index
    for index, task in enumerate(tasks):
        if not task.done and lowered in task.text.lower():
            return index
    return None


def _reply(board_ref: "StateRef[TripBoard]", index: int, note: str) -> TripBoardResponse:
    trip = board_ref.current.trips[index]
    return TripBoardResponse(
        trip_id=trip.trip_id,
        status=trip.status,
        remaining=[task.text for task in trip.remaining if not task.done],
        note=note,
        board=_render(board_ref.current),
    )


def _no_such_trip(board_ref: "StateRef[TripBoard]", trip_id: str) -> TripBoardResponse:
    """A miss is reported, never raised: a wrong id is the caller's to correct, and a
    tool error would end the turn over a bookkeeping slip."""
    return TripBoardResponse(
        trip_id=trip_id,
        status="unknown",
        remaining=[],
        note=(
            f"No trip {trip_id!r} on the board. Open one with open_trip, or pass "
            f'"latest" for the most recent.'
        ),
        board=_render(board_ref.current),
    )


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------
#
# All `inherently_safe=True`: writing to the agent's own notes cannot book anything, spend
# anything, or touch the outside world. The travel tools stay gated; see the approval
# policy in conversational_workflow.py.


@agent.tool_defn(inherently_safe=True)
async def open_trip(
    request: OpenTripRequest, board: agent.Injected[StateRef[TripBoard]]
) -> TripBoardResponse:
    """Put a new trip on the board and return its `trip_id`.

    Call this as soon as you know what trip you are working on — before searching, not
    after booking. `description` is one line a person would recognize ("SFO to JFK,
    Jul 1-5"); `traveler` is who it is for. `tasks` is the starting checklist; leave it
    empty for the standard one, or pass your own plan for this trip.

    Everything else on this board is addressed by the `trip_id` this returns.
    """
    trip_id = f"TRIP-{str(workflow.uuid4())[:6].upper()}"
    tasks = [text.strip() for text in request.tasks if text.strip()] or list(DEFAULT_TASKS)
    with board.mutate() as draft:
        draft.trips.append(
            Trip(
                trip_id=trip_id,
                description=request.description,
                traveler=request.traveler,
                remaining=[TripTask(text=text) for text in tasks],
            )
        )
    return _reply(board, len(board.current.trips) - 1, f"Opened {trip_id}.")


@agent.tool_defn(inherently_safe=True)
async def add_trip_tasks(
    request: TripTasksRequest, board: agent.Injected[StateRef[TripBoard]]
) -> TripBoardResponse:
    """Add items to a trip's checklist — things you have discovered it still needs.

    Use it when the trip turns out to want something the plan did not have (a second
    hotel, a visa check, a seat request). Pass `"latest"` as `trip_id` for the most
    recently opened trip.
    """
    index = _find_trip(board.current, request.trip_id)
    if index is None:
        return _no_such_trip(board, request.trip_id)
    wanted = [text.strip() for text in request.tasks if text.strip()]
    with board.mutate() as draft:
        for text in wanted:
            draft.trips[index].remaining.append(TripTask(text=text))
    return _reply(board, index, f"Added {len(wanted)} task(s).")


@agent.tool_defn(inherently_safe=True)
async def complete_trip_tasks(
    request: TripTasksRequest, board: agent.Injected[StateRef[TripBoard]]
) -> TripBoardResponse:
    """Tick items off a trip's checklist.

    Name each task by its text — the exact wording, or a distinctive part of it. Do this
    as soon as the work is actually done, not at the end of the conversation: the board is
    what the user is watching, and a checklist updated in one batch at the end was never a
    checklist.

    `record_booking` already ticks off what a booking finishes, so you do not need this for
    those.
    """
    index = _find_trip(board.current, request.trip_id)
    if index is None:
        return _no_such_trip(board, request.trip_id)

    matched: list[int] = []
    missed: list[str] = []
    tasks = list(board.current.trips[index].remaining)
    for wanted in request.tasks:
        found = _match_task(tasks, wanted)
        # Matched against a local copy that is updated as we go, so two names cannot
        # resolve to the same open task and silently tick one thing twice.
        if found is None or found in matched:
            missed.append(wanted)
            continue
        matched.append(found)
        tasks[found] = TripTask(text=tasks[found].text, done=True)

    # One block, so ticking three tasks is one version carrying three ops.
    with board.mutate() as draft:
        for position in matched:
            draft.trips[index].remaining[position].done = True

    note = f"Completed {len(matched)} task(s)."
    if missed:
        note += f" No open task matched: {', '.join(repr(text) for text in missed)}."
    return _reply(board, index, note)


@agent.tool_defn(inherently_safe=True)
async def record_booking(
    request: RecordBookingRequest, board: agent.Injected[StateRef[TripBoard]]
) -> TripBoardResponse:
    """Record a flight or hotel you have just booked, and tick off what it finishes.

    Call it immediately after `book_flight` / `book_hotel` returns, with the confirmation
    code that call gave you — never one you made up. `detail` is the line a person would
    want to read back: airline, flight number and times, or hotel name and nights.

    `completes` names the checklist items this booking satisfies; they are ticked in the
    same commit as the booking, so the board never shows a trip with the flight booked and
    "book the flight" still outstanding.
    """
    index = _find_trip(board.current, request.trip_id)
    if index is None:
        return _no_such_trip(board, request.trip_id)

    tasks = list(board.current.trips[index].remaining)
    matched: list[int] = []
    for wanted in request.completes:
        found = _match_task(tasks, wanted)
        if found is None or found in matched:
            continue
        matched.append(found)
        tasks[found] = TripTask(text=tasks[found].text, done=True)

    booking = Booking(
        kind=request.kind,
        confirmation_code=request.confirmation_code,
        detail=request.detail,
        price_usd=request.price_usd,
    )
    # The booking and the tasks it closes land in ONE mutate(), so a consumer of the state
    # stream never observes a moment where they disagree.
    with board.mutate() as draft:
        trip = draft.trips[index]
        if request.kind == "flight":
            trip.flights.append(booking)
        else:
            trip.hotels.append(booking)
        for position in matched:
            trip.remaining[position].done = True

    return _reply(
        board,
        index,
        f"Recorded {request.kind} {request.confirmation_code}"
        + (f"; completed {len(matched)} task(s)." if matched else "."),
    )


@agent.tool_defn(inherently_safe=True)
async def set_trip_status(
    request: TripStatusRequest, board: agent.Injected[StateRef[TripBoard]]
) -> TripBoardResponse:
    """Move a trip to `booked` once everything is arranged, or `cancelled` if it is off.

    Leave it `collecting` while there is anything left to do.
    """
    index = _find_trip(board.current, request.trip_id)
    if index is None:
        return _no_such_trip(board, request.trip_id)
    with board.mutate() as draft:
        draft.trips[index].status = request.status
    return _reply(board, index, f"Status is now {request.status}.")


@agent.tool_defn(inherently_safe=True)
async def read_trip_board(board: agent.Injected[StateRef[TripBoard]]) -> str:
    """Return the whole board: every trip, its bookings, and what each still needs.

    Takes no arguments. The board is durable workflow state, so this is how you recall
    where you left off at the start of a follow-up request.
    """
    return _render(board.current)


# Handed to `code_mode_tool` alongside the travel tools, and named here so the approval
# policy can allow exactly these by name without loosening anything else.
BOARD_TOOLS = [
    open_trip,
    add_trip_tasks,
    complete_trip_tasks,
    record_booking,
    set_trip_status,
    read_trip_board,
]

BOARD_TOOL_NAMES = [tool.__name__ for tool in BOARD_TOOLS]
