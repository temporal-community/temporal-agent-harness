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
