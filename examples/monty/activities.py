"""Simulated travel-booking activity implementations.

These activities return realistic-looking fake data so the sandbox workflow can be
exercised end-to-end without any real external APIs. The workflow exposes each one to
the sandboxed script as a *host function* (see ``workflow.py``); when the script calls
that host function, the workflow runs the matching activity here durably. The script
never touches Temporal — it just calls a plain function.

Each is an ``@agent.activity_tool_defn``, so when the workflow dispatches it (via
``run_tool``) the activity publishes its own ``tool_start``/``tool_end`` lifecycle events
on the turn stream — each host call the script makes shows up as a distinct tool
invocation, like any other harness tool. The decorated object is the in-workflow
dispatcher; ``agent.tool_activity(tool)`` returns the activity body the worker registers
(see ``ALL_ACTIVITIES``).

No ``from __future__ import annotations`` here, for consistency with the rest of the
agent's Temporal-facing modules (stringized annotations trip the pydantic converter).
"""

import asyncio
import hashlib
import random
from datetime import timedelta

from temporalio import activity
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent

from .travel_models import (
    Flight,
    FlightBookingRequest,
    FlightBookingResponse,
    FlightSearchRequest,
    FlightSearchResponse,
    Hotel,
    HotelBookingRequest,
    HotelBookingResponse,
    HotelSearchRequest,
    HotelSearchResponse,
    TripSummaryRequest,
    TripSummaryResponse,
)

# ---------------------------------------------------------------------------
# Simulated data
# ---------------------------------------------------------------------------

AIRLINES = ["United", "Delta", "American", "JetBlue", "Southwest", "Alaska"]
HOTEL_CHAINS = ["Marriott", "Hilton", "Hyatt", "IHG", "Best Western", "Radisson"]
NEIGHBORHOODS = {
    "New York": ["Midtown", "SoHo", "Upper East Side", "Chelsea", "Tribeca"],
    "San Francisco": ["Union Square", "Fisherman's Wharf", "SOMA", "Mission", "Nob Hill"],
    "Los Angeles": ["Hollywood", "Santa Monica", "Downtown", "Beverly Hills", "Venice"],
    "Chicago": ["The Loop", "River North", "Magnificent Mile", "Wicker Park", "Lincoln Park"],
    "Miami": ["South Beach", "Brickell", "Wynwood", "Coral Gables", "Coconut Grove"],
}
DEFAULT_NEIGHBORHOODS = ["Downtown", "City Center", "Old Town", "Waterfront", "Arts District"]

# Per-activity execution ceiling. Trivial for these simulated calls, but a real host
# function might do meaningful I/O; tune per tool if that ever matters. Lives on each
# tool's decorator now (the in-workflow approval/dispatch logic, if any, runs outside it).
_MONTY_ACTIVITY_TIMEOUT = timedelta(seconds=30)

# In-memory booking store so get_trip_summary can look things up. Lives in the worker
# process for the demo's lifetime — fine for a single-worker prototype.
_bookings: dict[str, dict] = {}


def _make_ref(prefix: str, *parts: str) -> str:
    """Deterministic but realistic-looking confirmation code."""
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:6].upper()
    return f"{prefix}-{digest}"


# ---------------------------------------------------------------------------
# Activity implementations
# ---------------------------------------------------------------------------


@agent.activity_tool_defn(
    name="search_flights",
    activity_config=ActivityConfig(start_to_close_timeout=_MONTY_ACTIVITY_TIMEOUT),
)
async def search_flights_activity(request: FlightSearchRequest) -> FlightSearchResponse:
    """Search for available flights between two airports on a given date."""
    activity.logger.info(
        "Searching flights %s -> %s on %s", request.origin, request.destination, request.date
    )

    # Simulate the latency of querying an external flight-search API.
    await asyncio.sleep(random.uniform(3.0, 5.0))

    rng = random.Random(f"{request.origin}{request.destination}{request.date}")
    num_results = rng.randint(2, 5)
    flights = []
    for i in range(num_results):
        airline = rng.choice(AIRLINES)
        dep_hour = rng.randint(6, 21)
        duration_hrs = rng.randint(2, 7)
        arr_hour = (dep_hour + duration_hrs) % 24
        stops = rng.choices([0, 1, 2], weights=[5, 3, 1])[0]
        price = round(rng.uniform(150, 800), 2)

        flights.append(
            Flight(
                flight_id=f"FL-{request.origin}{request.destination}-{i + 1:03d}",
                airline=airline,
                departure_time=f"{dep_hour:02d}:{rng.choice(['00', '15', '30', '45'])}",
                arrival_time=f"{arr_hour:02d}:{rng.choice(['00', '15', '30', '45'])}",
                price_usd=price,
                stops=stops,
            )
        )

    return FlightSearchResponse(flights=flights)


@agent.activity_tool_defn(
    name="search_hotels",
    activity_config=ActivityConfig(start_to_close_timeout=_MONTY_ACTIVITY_TIMEOUT),
)
async def search_hotels_activity(request: HotelSearchRequest) -> HotelSearchResponse:
    """Search for available hotels in a city for the given date range."""
    activity.logger.info(
        "Searching hotels in %s (%s to %s)", request.city, request.check_in, request.check_out
    )

    # Simulate the latency of querying an external hotel-search API.
    await asyncio.sleep(random.uniform(3.0, 5.0))

    rng = random.Random(f"{request.city}{request.check_in}{request.check_out}")
    neighborhoods = NEIGHBORHOODS.get(request.city, DEFAULT_NEIGHBORHOODS)
    num_results = rng.randint(2, 5)
    hotels = []
    for i in range(num_results):
        chain = rng.choice(HOTEL_CHAINS)
        neighborhood = rng.choice(neighborhoods)
        stars = rng.randint(2, 5)
        price = round(rng.uniform(80, 450), 2)

        hotels.append(
            Hotel(
                hotel_id=f"HT-{request.city[:3].upper()}-{i + 1:03d}",
                name=f"{chain} {neighborhood}",
                star_rating=stars,
                price_per_night_usd=price,
                neighborhood=neighborhood,
            )
        )

    return HotelSearchResponse(hotels=hotels)


@agent.activity_tool_defn(
    name="book_flight",
    activity_config=ActivityConfig(start_to_close_timeout=_MONTY_ACTIVITY_TIMEOUT),
)
async def book_flight_activity(request: FlightBookingRequest) -> FlightBookingResponse:
    """Book a specific flight for a passenger and return a confirmation code."""
    activity.logger.info("Booking flight %s for %s", request.flight_id, request.passenger_name)

    # Simulate the latency of confirming the booking with the airline.
    await asyncio.sleep(random.uniform(4.0, 6.0))

    ref = _make_ref("AIR", request.flight_id, request.passenger_name)
    _bookings[ref] = {
        "type": "flight",
        "flight_id": request.flight_id,
        "passenger_name": request.passenger_name,
    }

    return FlightBookingResponse(
        confirmation_code=ref,
        flight_id=request.flight_id,
        passenger_name=request.passenger_name,
        status="confirmed",
    )


@agent.activity_tool_defn(
    name="book_hotel",
    activity_config=ActivityConfig(start_to_close_timeout=_MONTY_ACTIVITY_TIMEOUT),
)
async def book_hotel_activity(request: HotelBookingRequest) -> HotelBookingResponse:
    """Book a specific hotel for a guest and return a confirmation code."""
    activity.logger.info("Booking hotel %s for %s", request.hotel_id, request.guest_name)

    # Simulate the latency of confirming the booking with the hotel.
    await asyncio.sleep(random.uniform(4.0, 6.0))

    ref = _make_ref("HTL", request.hotel_id, request.guest_name)
    _bookings[ref] = {
        "type": "hotel",
        "hotel_id": request.hotel_id,
        "guest_name": request.guest_name,
    }

    return HotelBookingResponse(
        confirmation_code=ref,
        hotel_id=request.hotel_id,
        guest_name=request.guest_name,
        status="confirmed",
    )


@agent.activity_tool_defn(
    name="get_trip_summary",
    activity_config=ActivityConfig(start_to_close_timeout=_MONTY_ACTIVITY_TIMEOUT),
)
async def get_trip_summary_activity(request: TripSummaryRequest) -> TripSummaryResponse:
    """Build a human-readable trip itinerary from a list of booking confirmation codes."""
    activity.logger.info("Generating trip summary for %d bookings", len(request.booking_refs))

    # Simulate the latency of assembling the itinerary.
    await asyncio.sleep(random.uniform(1.5, 4.5))

    lines = ["Trip Itinerary", "=" * 40]
    for ref in request.booking_refs:
        booking = _bookings.get(ref)
        if booking is None:
            lines.append(f"  [{ref}] — booking not found")
        elif booking["type"] == "flight":
            lines.append(
                f"  Flight {booking['flight_id']} — "
                f"Passenger: {booking['passenger_name']} — "
                f"Confirmation: {ref}"
            )
        elif booking["type"] == "hotel":
            lines.append(
                f"  Hotel {booking['hotel_id']} — "
                f"Guest: {booking['guest_name']} — "
                f"Confirmation: {ref}"
            )
    lines.append("=" * 40)

    return TripSummaryResponse(summary="\n".join(lines))


# Convenience list for registering all activities with a Temporal worker — the durable
# activity body of each tool (the module-level names above are the in-workflow dispatchers
# the workflow calls via run_tool; tool_activity() returns the activity defn to register).
ALL_ACTIVITIES = [
    agent.tool_activity(t)
    for t in (
        search_flights_activity,
        search_hotels_activity,
        book_flight_activity,
        book_hotel_activity,
        get_trip_summary_activity,
    )
]
