"""Accepted message types for the Monty dynamic agent.

NB: no ``from __future__ import annotations`` here — these cross Temporal's pydantic
converter, and stringized annotations leave the discriminated-union machinery "not fully
defined". Keep annotations concrete.
"""

from pydantic import BaseModel


class RunScript(BaseModel):
    r"""Execute a Python ``script`` inside the Monty sandbox for this turn.

    The script is **not** plain CPython — it runs in `pydantic-monty
    <https://pypi.org/project/pydantic-monty/>`_, a sandboxed interpreter. It has no
    filesystem, network, or arbitrary imports — only ``asyncio`` plus the host functions
    listed below, which the workflow backs with durable Temporal activities. Everything
    else is ordinary in-sandbox Python (arithmetic, comprehensions, f-strings, ``print`` —
    captured and returned to you).

    The value of the script's **last expression** becomes the turn's reply (alongside
    anything it printed). So end your script with the value you want back.

    The host functions are ``async`` — you MUST ``await`` them — so structure the script as::

        import asyncio
        async def main():
            ...                 # await host functions here
            return <final value>
        asyncio.run(main())

    **Concurrency:** independent host calls should run AT THE SAME TIME via
    ``asyncio.gather`` — a gathered batch is executed concurrently (each call its own
    durable activity), so don't ``await`` them one-by-one unless a later call needs an
    earlier call's result.

    ──────────────────────────────────────────────────────────────────────────────
    HOST FUNCTIONS AVAILABLE TO THE SCRIPT (a simulated travel-booking backend)
    ──────────────────────────────────────────────────────────────────────────────
    All are ``async`` (await them). Search results come back as ``list[dict]`` and
    bookings as ``dict``, so index into them with normal Python
    (``flights[0]["price_usd"]``, ``min(...)``, etc.).

      • ``async search_flights(origin: str, destination: str, date: str) -> list[dict]``
            Search flights between two airport codes on a date (``"YYYY-MM-DD"``).
            Each flight dict has keys:
              ``flight_id`` (str), ``airline`` (str), ``departure_time`` (str "HH:MM"),
              ``arrival_time`` (str "HH:MM"), ``price_usd`` (float), ``stops`` (int).

      • ``async search_hotels(city: str, check_in: str, check_out: str) -> list[dict]``
            Search hotels in a city for a date range (dates ``"YYYY-MM-DD"``).
            Each hotel dict has keys:
              ``hotel_id`` (str), ``name`` (str), ``star_rating`` (int),
              ``price_per_night_usd`` (float), ``neighborhood`` (str).

      • ``async book_flight(flight_id: str, passenger_name: str) -> dict``
            Book a flight (use a ``flight_id`` returned by ``search_flights``).
            Returns a dict with keys:
              ``confirmation_code`` (str), ``flight_id`` (str),
              ``passenger_name`` (str), ``status`` (str, e.g. "confirmed").

      • ``async book_hotel(hotel_id: str, guest_name: str) -> dict``
            Book a hotel (use a ``hotel_id`` returned by ``search_hotels``).
            Returns a dict with keys:
              ``confirmation_code`` (str), ``hotel_id`` (str),
              ``guest_name`` (str), ``status`` (str, e.g. "confirmed").

      • ``async get_trip_summary(booking_refs: list[str]) -> str``
            Build a human-readable itinerary from confirmation codes (the
            ``confirmation_code`` values returned by ``book_flight`` / ``book_hotel``).
            Returns a formatted multi-line string.

    No other names are injected. There are no ``inputs`` — bind any values you need as
    literals in the script itself.

    ──────────────────────────────────────────────────────────────────────────────
    EXAMPLE script — concurrently search, then book, then summarize
    ──────────────────────────────────────────────────────────────────────────────
        import asyncio
        async def main():
            flights, hotels = await asyncio.gather(
                search_flights("SFO", "JFK", "2026-07-01"),
                search_hotels("New York", "2026-07-01", "2026-07-05"),
            )
            cheapest = min(flights, key=lambda f: f["price_usd"])
            nicest = max(hotels, key=lambda h: h["star_rating"])
            flight, hotel = await asyncio.gather(
                book_flight(cheapest["flight_id"], "Ada Lovelace"),
                book_hotel(nicest["hotel_id"], "Ada Lovelace"),
            )
            print(f"booked {cheapest['airline']} at ${cheapest['price_usd']}")
            return await get_trip_summary(
                [flight["confirmation_code"], hotel["confirmation_code"]]
            )
        asyncio.run(main())

    The reply for that turn would carry the printed line plus the itinerary string
    (the script's final expression).
    """

    script: str
