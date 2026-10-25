// Domain models for the simulated travel-booking activities (Python's travel_models.py).
//
// Request/response pairs that cross the Temporal activity boundary. Distinct from models.ts, which
// holds the agent's accepted *message* type (RunScript). Each is titled with Python's class name,
// which is the TypedDict name a Code Mode script is type-checked against.

import { z } from "zod";

// ---------------------------------------------------------------------------------------------
// Flights
// ---------------------------------------------------------------------------------------------

export const Flight = z
  .object({
    flight_id: z.string(),
    airline: z.string(),
    departure_time: z.string(),
    arrival_time: z.string(),
    price_usd: z.number(),
    stops: z.int(),
  })
  .meta({ title: "Flight" });

export const FlightSearchRequest = z
  .object({ origin: z.string(), destination: z.string(), date: z.string() })
  .meta({ title: "FlightSearchRequest" });

export const FlightSearchResponse = z
  .object({ flights: z.array(Flight) })
  .meta({ title: "FlightSearchResponse" });

export const FlightBookingRequest = z
  .object({ flight_id: z.string(), passenger_name: z.string() })
  .meta({ title: "FlightBookingRequest" });

export const FlightBookingResponse = z
  .object({
    confirmation_code: z.string(),
    flight_id: z.string(),
    passenger_name: z.string(),
    status: z.string(),
  })
  .meta({ title: "FlightBookingResponse" });

// ---------------------------------------------------------------------------------------------
// Hotels
// ---------------------------------------------------------------------------------------------

export const Hotel = z
  .object({
    hotel_id: z.string(),
    name: z.string(),
    star_rating: z.int(),
    price_per_night_usd: z.number(),
    neighborhood: z.string(),
  })
  .meta({ title: "Hotel" });

export const HotelSearchRequest = z
  .object({ city: z.string(), check_in: z.string(), check_out: z.string() })
  .meta({ title: "HotelSearchRequest" });

export const HotelSearchResponse = z
  .object({ hotels: z.array(Hotel) })
  .meta({ title: "HotelSearchResponse" });

export const HotelBookingRequest = z
  .object({ hotel_id: z.string(), guest_name: z.string() })
  .meta({ title: "HotelBookingRequest" });

export const HotelBookingResponse = z
  .object({
    confirmation_code: z.string(),
    hotel_id: z.string(),
    guest_name: z.string(),
    status: z.string(),
  })
  .meta({ title: "HotelBookingResponse" });

// ---------------------------------------------------------------------------------------------
// Trip summary
// ---------------------------------------------------------------------------------------------

export const TripSummaryRequest = z
  .object({ booking_refs: z.array(z.string()) })
  .meta({ title: "TripSummaryRequest" });

export const TripSummaryResponse = z
  .object({ summary: z.string() })
  .meta({ title: "TripSummaryResponse" });

export type Flight = z.output<typeof Flight>;
export type Hotel = z.output<typeof Hotel>;
