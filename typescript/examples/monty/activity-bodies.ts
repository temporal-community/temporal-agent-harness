// Simulated travel-booking activity implementations (the body half of Python's activities.py).
//
// These return realistic-looking fake data so the agents can be exercised end to end without any
// real external APIs. Each workflow exposes the matching tool in activities.ts to the sandboxed
// script as a host function; when the script calls it, the workflow runs the body here durably.
// The script never touches Temporal: it just calls a plain function.

import { createHash } from "node:crypto";
import { setTimeout as sleep } from "node:timers/promises";
import { log } from "@temporalio/activity";
import { toolActivity } from "@temporalio/agent-harness-worker";
import {
  bookFlight,
  bookHotel,
  getTripSummary,
  searchFlights,
  searchHotels,
} from "./activities.ts";
import type { Flight, Hotel } from "./travel-models.ts";

// ---------------------------------------------------------------------------------------------
// Simulated data
// ---------------------------------------------------------------------------------------------

const AIRLINES = ["United", "Delta", "American", "JetBlue", "Southwest", "Alaska"];
const HOTEL_CHAINS = ["Marriott", "Hilton", "Hyatt", "IHG", "Best Western", "Radisson"];
const NEIGHBORHOODS: Record<string, string[]> = {
  "New York": ["Midtown", "SoHo", "Upper East Side", "Chelsea", "Tribeca"],
  "San Francisco": ["Union Square", "Fisherman's Wharf", "SOMA", "Mission", "Nob Hill"],
  "Los Angeles": ["Hollywood", "Santa Monica", "Downtown", "Beverly Hills", "Venice"],
  Chicago: ["The Loop", "River North", "Magnificent Mile", "Wicker Park", "Lincoln Park"],
  Miami: ["South Beach", "Brickell", "Wynwood", "Coral Gables", "Coconut Grove"],
};
const DEFAULT_NEIGHBORHOODS = [
  "Downtown",
  "City Center",
  "Old Town",
  "Waterfront",
  "Arts District",
];

type BookingRecord =
  | { type: "flight"; flight_id: string; passenger_name: string }
  | { type: "hotel"; hotel_id: string; guest_name: string };

// In-memory booking store so get_trip_summary can look things up. Lives in the worker process for
// the demo's lifetime: fine for a single-worker prototype.
const bookings = new Map<string, BookingRecord>();

/** Deterministic but realistic-looking confirmation code. */
function makeRef(prefix: string, ...parts: string[]): string {
  const digest = createHash("sha256").update(parts.join("|")).digest("hex");
  return `${prefix}-${digest.slice(0, 6).toUpperCase()}`;
}

/** A random source seeded by a string, so the same search returns the same results. */
class SeededRandom {
  #state: number;

  constructor(seed: string) {
    this.#state = createHash("sha256").update(seed).digest().readUInt32LE(0);
  }

  /** In [0, 1): mulberry32. */
  random(): number {
    this.#state = (this.#state + 0x6d2b79f5) | 0;
    let t = this.#state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  }

  /** An integer in [low, high], both included. */
  randint(low: number, high: number): number {
    return low + Math.floor(this.random() * (high - low + 1));
  }

  uniform(low: number, high: number): number {
    return low + this.random() * (high - low);
  }

  choice<T>(items: readonly T[]): T {
    return items[Math.floor(this.random() * items.length)]!;
  }

  weighted<T>(items: readonly T[], weights: readonly number[]): T {
    let pick = this.random() * weights.reduce((a, b) => a + b, 0);
    for (const [i, weight] of weights.entries()) {
      pick -= weight;
      if (pick < 0) return items[i]!;
    }
    return items.at(-1)!;
  }
}

const round2 = (n: number) => Math.round(n * 100) / 100;
const pad2 = (n: number) => String(n).padStart(2, "0");

/** Simulate the latency of an external API: between `low` and `high` seconds. */
const latency = (low: number, high: number) => sleep((low + Math.random() * (high - low)) * 1000);

// ---------------------------------------------------------------------------------------------
// Activity implementations
// ---------------------------------------------------------------------------------------------

/** The bodies of the tools in activities.ts, for the worker's `AgentHarnessPlugin({ tools })`. */
export const TOOL_ACTIVITIES = [
  toolActivity(searchFlights, async ({ request }) => {
    log.info(`Searching flights ${request.origin} -> ${request.destination} on ${request.date}`);

    // Simulate the latency of querying an external flight-search API.
    await latency(3.0, 5.0);

    const rng = new SeededRandom(`${request.origin}${request.destination}${request.date}`);
    const flights: Flight[] = [];
    for (let i = 0, n = rng.randint(2, 5); i < n; i++) {
      const airline = rng.choice(AIRLINES);
      const depHour = rng.randint(6, 21);
      const arrHour = (depHour + rng.randint(2, 7)) % 24;
      const stops = rng.weighted([0, 1, 2], [5, 3, 1]);
      const price = round2(rng.uniform(150, 800));
      flights.push({
        flight_id: `FL-${request.origin}${request.destination}-${String(i + 1).padStart(3, "0")}`,
        airline,
        departure_time: `${pad2(depHour)}:${rng.choice(["00", "15", "30", "45"])}`,
        arrival_time: `${pad2(arrHour)}:${rng.choice(["00", "15", "30", "45"])}`,
        price_usd: price,
        stops,
      });
    }
    return { flights };
  }),

  toolActivity(searchHotels, async ({ request }) => {
    log.info(`Searching hotels in ${request.city} (${request.check_in} to ${request.check_out})`);

    // Simulate the latency of querying an external hotel-search API.
    await latency(3.0, 5.0);

    const rng = new SeededRandom(`${request.city}${request.check_in}${request.check_out}`);
    const neighborhoods = NEIGHBORHOODS[request.city] ?? DEFAULT_NEIGHBORHOODS;
    const hotels: Hotel[] = [];
    for (let i = 0, n = rng.randint(2, 5); i < n; i++) {
      const chain = rng.choice(HOTEL_CHAINS);
      const neighborhood = rng.choice(neighborhoods);
      hotels.push({
        hotel_id: `HT-${request.city.slice(0, 3).toUpperCase()}-${String(i + 1).padStart(3, "0")}`,
        name: `${chain} ${neighborhood}`,
        star_rating: rng.randint(2, 5),
        price_per_night_usd: round2(rng.uniform(80, 450)),
        neighborhood,
      });
    }
    return { hotels };
  }),

  toolActivity(bookFlight, async ({ request }) => {
    log.info(`Booking flight ${request.flight_id} for ${request.passenger_name}`);

    // Simulate the latency of confirming the booking with the airline.
    await latency(4.0, 6.0);

    const ref = makeRef("AIR", request.flight_id, request.passenger_name);
    bookings.set(ref, {
      type: "flight",
      flight_id: request.flight_id,
      passenger_name: request.passenger_name,
    });
    return {
      confirmation_code: ref,
      flight_id: request.flight_id,
      passenger_name: request.passenger_name,
      status: "confirmed",
    };
  }),

  toolActivity(bookHotel, async ({ request }) => {
    log.info(`Booking hotel ${request.hotel_id} for ${request.guest_name}`);

    // Simulate the latency of confirming the booking with the hotel.
    await latency(4.0, 6.0);

    const ref = makeRef("HTL", request.hotel_id, request.guest_name);
    bookings.set(ref, {
      type: "hotel",
      hotel_id: request.hotel_id,
      guest_name: request.guest_name,
    });
    return {
      confirmation_code: ref,
      hotel_id: request.hotel_id,
      guest_name: request.guest_name,
      status: "confirmed",
    };
  }),

  toolActivity(getTripSummary, async ({ request }) => {
    log.info(`Generating trip summary for ${request.booking_refs.length} bookings`);

    // Simulate the latency of assembling the itinerary.
    await latency(1.5, 4.5);

    const rule = "=".repeat(40);
    const lines = ["Trip Itinerary", rule];
    for (const ref of request.booking_refs) {
      const booking = bookings.get(ref);
      if (booking === undefined) lines.push(`  [${ref}] — booking not found`);
      else if (booking.type === "flight")
        lines.push(
          `  Flight ${booking.flight_id} — Passenger: ${booking.passenger_name} — Confirmation: ${ref}`,
        );
      else
        lines.push(
          `  Hotel ${booking.hotel_id} — Guest: ${booking.guest_name} — Confirmation: ${ref}`,
        );
    }
    lines.push(rule);
    return { summary: lines.join("\n") };
  }),
];
