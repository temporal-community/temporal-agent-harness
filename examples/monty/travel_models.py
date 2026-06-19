"""Domain models for the simulated travel-booking activities.

Request/response pairs that cross the Temporal activity boundary (pydantic, so the
project's pydantic data converter serializes them). Distinct from ``models.py``, which
holds the agent's accepted *message* type (:class:`RunScript`).

No ``from __future__ import annotations`` — these cross Temporal's pydantic converter,
and stringized annotations on nested models trip its TypeAdapter build. Concrete
annotations, nested models defined before the responses that reference them.
"""

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Flights
# ---------------------------------------------------------------------------


class Flight(BaseModel):
    flight_id: str
    airline: str
    departure_time: str
    arrival_time: str
    price_usd: float
    stops: int


class FlightSearchRequest(BaseModel):
    origin: str
    destination: str
    date: str


class FlightSearchResponse(BaseModel):
    flights: list[Flight]


class FlightBookingRequest(BaseModel):
    flight_id: str
    passenger_name: str


class FlightBookingResponse(BaseModel):
    confirmation_code: str
    flight_id: str
    passenger_name: str
    status: str


# ---------------------------------------------------------------------------
# Hotels
# ---------------------------------------------------------------------------


class Hotel(BaseModel):
    hotel_id: str
    name: str
    star_rating: int
    price_per_night_usd: float
    neighborhood: str


class HotelSearchRequest(BaseModel):
    city: str
    check_in: str
    check_out: str


class HotelSearchResponse(BaseModel):
    hotels: list[Hotel]


class HotelBookingRequest(BaseModel):
    hotel_id: str
    guest_name: str


class HotelBookingResponse(BaseModel):
    confirmation_code: str
    hotel_id: str
    guest_name: str
    status: str


# ---------------------------------------------------------------------------
# Trip summary
# ---------------------------------------------------------------------------


class TripSummaryRequest(BaseModel):
    booking_refs: list[str]


class TripSummaryResponse(BaseModel):
    summary: str


# ---------------------------------------------------------------------------
# Monty type-check stubs for the host functions
# ---------------------------------------------------------------------------
#
# Monty can statically type-check a script BEFORE running it, given stub source that
# declares the names the script may call (see ``monty.Monty(..., type_check=True,
# type_check_stubs=...)``). The host functions are external (the sandbox suspends and the
# workflow services them), so without stubs Monty can't know their signatures or return
# shapes — and can't catch a bad call (wrong arg type, unknown result key) until runtime.
#
# These stubs give Monty exactly those signatures. The result shapes are TypedDicts that
# mirror the models above, so the checker also validates dict-key access on results (e.g.
# ``flight["price_usd"]`` is OK, ``flight["cost"]`` is an error). The stubs are prepended
# ONLY for type-checking — they do NOT define the functions at runtime, so each call still
# suspends as an external host call exactly as before.
#
# KEEP IN SYNC with the Flight/Hotel/*BookingResponse models above and with the host-call
# mapping in the workflow's ``_dispatch_host_call`` (which is what actually returns these).
HOST_FUNCTION_STUBS = """\
from typing import TypedDict

class Flight(TypedDict):
    flight_id: str
    airline: str
    departure_time: str
    arrival_time: str
    price_usd: float
    stops: int

class Hotel(TypedDict):
    hotel_id: str
    name: str
    star_rating: int
    price_per_night_usd: float
    neighborhood: str

class FlightBooking(TypedDict):
    confirmation_code: str
    flight_id: str
    passenger_name: str
    status: str

class HotelBooking(TypedDict):
    confirmation_code: str
    hotel_id: str
    guest_name: str
    status: str

async def search_flights(origin: str, destination: str, date: str) -> list[Flight]: ...
async def search_hotels(city: str, check_in: str, check_out: str) -> list[Hotel]: ...
async def book_flight(flight_id: str, passenger_name: str) -> FlightBooking: ...
async def book_hotel(hotel_id: str, guest_name: str) -> HotelBooking: ...
async def get_trip_summary(booking_refs: list[str]) -> str: ...
"""
