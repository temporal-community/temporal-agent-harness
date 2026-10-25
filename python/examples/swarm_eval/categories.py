"""What the eval swarm tests a travel agent on: one board lane, one lead and one rubric each.

Each category has a BRIEF, which tells a case writer what kind of conversation to invent, and a
RUBRIC, which tells a judge what passing means. The rubrics ask about what the agent DID — its
tool calls and their results are in front of the judge — so a reply that sounds right but rests
on a search it never ran, or a confirmation code no booking returned, fails.
"""

from __future__ import annotations

from examples.swarm_eval.models import Category


CATEGORIES: tuple[Category, ...] = (
    Category(
        key="flight_search",
        title="Flight search",
        brief="The user wants to see flight options between two airports (IATA codes) on one "
        "date, and does not want anything booked.",
        rubric="Searched flights with the user's origin, destination and date; every option it "
        "reported appears in that search's results; it booked nothing.",
    ),
    Category(
        key="flight_booking",
        title="Book a flight",
        brief="The user wants one flight booked and states one preference that the search "
        "results can decide (cheapest, nonstop, or earliest departure). They give their name.",
        rubric="Booked a flight that is in the search results and best satisfies the stated "
        "preference among them, for the user's name, and reported the confirmation code the "
        "booking returned.",
    ),
    Category(
        key="hotel_booking",
        title="Book a hotel",
        brief="The user wants a hotel in a city for given check-in and check-out dates, with "
        "one preference (cheapest, or highest rated). They give their name.",
        rubric="Searched hotels for the city and dates, booked one from the results that "
        "satisfies the preference, and reported the confirmation code the booking returned.",
    ),
    Category(
        key="full_trip",
        title="Flight and hotel",
        brief="The user wants a whole trip: a flight there and a hotel for the stay, booked in "
        "one conversation. They give their name and dates.",
        rubric="Booked both a flight and a hotel from search results, with dates consistent "
        "with each other, recorded both on the trip board with their confirmation codes, and "
        "set the trip's status to booked.",
    ),
    Category(
        key="clarification",
        title="Missing details",
        brief="The user's first message leaves out one essential detail (the travel date, the "
        "origin, or their name for a booking). They supply it only when asked.",
        rubric="Asked for the missing detail instead of guessing it, then completed the request "
        "with the detail the user gave. Booking or searching with an invented value fails.",
    ),
    Category(
        key="change_of_plans",
        title="Change of plans",
        brief="The user starts a request, then in their second message changes one detail "
        "(destination or date) before anything is booked.",
        rubric="Acted on the changed detail: any search or booking after the change uses the "
        "new value, and nothing was booked with the old one.",
    ),
    Category(
        key="budget",
        title="Budget limit",
        brief="The user wants a flight booked but only under a stated maximum price in USD. "
        "Choose budgets that sometimes are and sometimes are not achievable.",
        rubric="Did not book anything priced above the budget according to the search results; "
        "if no result fit, said so and did not book.",
    ),
    Category(
        key="out_of_scope",
        title="Out of scope",
        brief="Alongside a normal travel request, or instead of one, the user asks for "
        "something a flight-and-hotel agent cannot do: a car rental, a refund, visa advice.",
        rubric="Declined or redirected the unsupported part honestly, handled any supported "
        "part, and never claimed a capability or produced a confirmation code it did not get "
        "from a tool.",
    ),
)

BY_KEY = {category.key: category for category in CATEGORIES}
