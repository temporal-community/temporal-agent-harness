// The simulated travel-booking tools, as the workflows see them (the declaration half of Python's
// activities.py).
//
// Each is an `activityTool`: when the workflow dispatches it (via `runTool`) it runs as a durable
// activity that publishes its own tool_start/tool_end on the turn stream, so each host call a
// script makes shows up as a distinct tool invocation. The bodies, which return realistic-looking
// fake data, are in activity-bodies.ts: they run on the worker, so they stay out of the workflow
// bundle.
//
// Each tool takes ONE request object, as Python's do, so a script calls
// `search_flights({"origin": ..., ...})` in either language.

import { activityTool } from "@temporalio/agent-harness";
import { z } from "zod";
import {
  FlightBookingRequest,
  FlightBookingResponse,
  FlightSearchRequest,
  FlightSearchResponse,
  HotelBookingRequest,
  HotelBookingResponse,
  HotelSearchRequest,
  HotelSearchResponse,
  TripSummaryRequest,
  TripSummaryResponse,
} from "./travel-models.ts";

// Per-activity execution ceiling. Trivial for these simulated calls, but a real host function
// might do meaningful I/O; tune per tool if that ever matters.
const MONTY_ACTIVITY_TIMEOUT = "30 seconds";
const activityOptions = { startToCloseTimeout: MONTY_ACTIVITY_TIMEOUT };

export const searchFlights = activityTool({
  name: "search_flights",
  description: "Search for available flights between two airports on a given date.",
  inputSchema: z.object({ request: FlightSearchRequest }),
  outputSchema: FlightSearchResponse,
  activityOptions,
});

export const searchHotels = activityTool({
  name: "search_hotels",
  description: "Search for available hotels in a city for the given date range.",
  inputSchema: z.object({ request: HotelSearchRequest }),
  outputSchema: HotelSearchResponse,
  activityOptions,
});

export const bookFlight = activityTool({
  name: "book_flight",
  description: "Book a specific flight for a passenger and return a confirmation code.",
  inputSchema: z.object({ request: FlightBookingRequest }),
  outputSchema: FlightBookingResponse,
  activityOptions,
});

export const bookHotel = activityTool({
  name: "book_hotel",
  description: "Book a specific hotel for a guest and return a confirmation code.",
  inputSchema: z.object({ request: HotelBookingRequest }),
  outputSchema: HotelBookingResponse,
  activityOptions,
});

export const getTripSummary = activityTool({
  name: "get_trip_summary",
  description: "Build a human-readable trip itinerary from a list of booking confirmation codes.",
  inputSchema: z.object({ request: TripSummaryRequest }),
  outputSchema: TripSummaryResponse,
  activityOptions,
});

// The agent's toolset, declared once. Each workflow wraps this list in a Code Mode tool (exposing
// them to the script as host functions), and the worker registers each one's body
// (activity-bodies.ts).
export const ALL_TOOLS = [searchFlights, searchHotels, bookFlight, bookHotel, getTripSummary];
