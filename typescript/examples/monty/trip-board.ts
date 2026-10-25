// The agent's running TODO board of trips, as OBSERVABLE state (Python's trip_board.py).
//
// The travel tools in activities.ts do the work; this module is the agent's memory of what it is
// collecting: one entry per trip, with the description, the flights and hotels booked so far, and
// what is still left to do.
//
// It is deliberately *not* an activity. The board is the agent's own notes, not an action on the
// outside world, so every tool here is an inline `tool` that runs in the workflow and writes to a
// StateRef the workflow owns. That is what makes it observable: the agent declares it once, as
// `states: { trip_board: state(TripBoard) }`, and from then on the harness publishes a snapshot and
// then one RFC 6902 patch per committed `mutate()` onto the agent's turn_events stream. The
// console's AGENT STATE pane folds those back into the document and marks exactly what each
// commit touched.
//
// Each tool takes ONE request object and returns ONE response object, like the travel tools, so
// every host function a script calls has the same shape. The `board` parameter is injected: the
// workflow supplies it per call and it is hidden from the model and from the generated Code Mode
// stubs, so a script never passes it and cannot address it.

import { uuid4 } from "@temporalio/workflow";
import { StateRef, injected, tool } from "@temporalio/agent-harness";
import { z } from "zod";

const TripStatus = z.enum(["collecting", "booked", "cancelled"]);
const BookingKind = z.enum(["flight", "hotel"]);

// What a trip starts out owing when the caller does not say. Seeded rather than left empty so the
// board is useful the moment a trip is opened, and so "what's left" is a real list the agent ticks
// down rather than something it has to invent first.
const DEFAULT_TASKS = [
  "Confirm dates and traveler",
  "Search and book the outbound flight",
  "Search and book the return flight",
  "Search and book the hotel",
  "Send the itinerary",
];

// ---------------------------------------------------------------------------------------------
// The observable state
// ---------------------------------------------------------------------------------------------
//
// Every field below is addressable by JSON pointer, which is the whole point: ticking one task
// emits `replace /trips/0/remaining/2/done`, not a re-send of the trip.

const TripTask = z
  .strictObject({ text: z.string(), done: z.boolean().default(false) })
  .meta({
    title: "TripTask",
    description: "One thing the trip still needs, and whether it has been done.",
  });

const Booking = z
  .strictObject({
    kind: BookingKind,
    confirmation_code: z.string().meta({
      description: "The code a `book_flight` / `book_hotel` host call returned. Never invented.",
    }),
    detail: z.string().meta({
      description:
        "Human-readable: airline, times, hotel name, nights — whatever the reader needs.",
    }),
    price_usd: z.number().default(0),
  })
  .meta({ title: "Booking", description: "A flight or hotel this trip has actually booked." });

const Trip = z
  .strictObject({
    trip_id: z.string(),
    description: z.string(),
    traveler: z.string(),
    status: TripStatus.default("collecting"),
    flights: z.array(Booking).default([]),
    hotels: z.array(Booking).default([]),
    remaining: z.array(TripTask).default([]),
  })
  .meta({ title: "Trip", description: "One trip the agent is collecting." });

export const TripBoard = z
  .strictObject({ trips: z.array(Trip).default([]) })
  .meta({ title: "TripBoard", description: "Every trip this session is working on, newest last." });

type TripTask = z.output<typeof TripTask>;
type Booking = z.output<typeof Booking>;
export type TripBoard = z.output<typeof TripBoard>;
type BoardRef = StateRef<TripBoard>;

// ---------------------------------------------------------------------------------------------
// Request / response shapes
// ---------------------------------------------------------------------------------------------

const OpenTripRequest = z
  .object({
    description: z.string(),
    traveler: z.string(),
    tasks: z
      .array(z.string())
      .default([])
      .meta({ description: "The checklist to start with. Empty means the standard one." }),
  })
  .meta({ title: "OpenTripRequest" });

const TripTasksRequest = z
  .object({ trip_id: z.string(), tasks: z.array(z.string()) })
  .meta({ title: "TripTasksRequest" });

const RecordBookingRequest = z
  .object({
    trip_id: z.string(),
    kind: BookingKind,
    confirmation_code: z.string(),
    detail: z.string(),
    price_usd: z.number().default(0),
    completes: z
      .array(z.string())
      .default([])
      .meta({
        description: "Checklist items this booking finishes, ticked off in the same commit.",
      }),
  })
  .meta({ title: "RecordBookingRequest" });

const TripStatusRequest = z
  .object({ trip_id: z.string(), status: TripStatus })
  .meta({ title: "TripStatusRequest" });

const TripBoardResponse = z
  .object({
    trip_id: z.string(),
    status: z.string(),
    remaining: z.array(z.string()),
    note: z.string().meta({
      description: "What the call did — including anything it was asked for and could not find.",
    }),
    board: z.string().meta({
      description:
        "The whole board, rendered, so the caller always sees the result of its own write.",
    }),
  })
  .meta({ title: "TripBoardResponse" });

type TripBoardResponse = z.output<typeof TripBoardResponse>;

// ---------------------------------------------------------------------------------------------
// Helpers (pure; no state access)
// ---------------------------------------------------------------------------------------------

/** `1234.5` as `1,234.50`. */
function money(amount: number): string {
  const [whole, cents] = amount.toFixed(2).split(".");
  return `${whole!.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${cents}`;
}

function render(board: TripBoard): string {
  if (board.trips.length === 0) return "(no trips on the board yet)";
  const lines: string[] = [];
  for (const trip of board.trips) {
    lines.push(`[${trip.trip_id}] ${trip.description} — ${trip.traveler} (${trip.status})`);
    for (const booking of [...trip.flights, ...trip.hotels]) {
      const price = booking.price_usd ? ` $${money(booking.price_usd)}` : "";
      lines.push(`    ${booking.kind}: ${booking.detail} — ${booking.confirmation_code}${price}`);
    }
    const left = trip.remaining.filter((task) => !task.done).map((task) => task.text);
    lines.push(`    remaining: ${left.length ? left.join("; ") : "(nothing — trip is complete)"}`);
  }
  return lines.join("\n");
}

/**
 * The index of `tripId`, or of the newest trip when it is blank or `latest`.
 *
 * The convenience matters: a script that just opened a trip has the id, but a model resuming a
 * conversation three turns later often does not, and the alternative is that it guesses one, which
 * silently writes nothing.
 */
function findTrip(board: TripBoard, tripId: string): number | null {
  const wanted = tripId.trim();
  if (!wanted || wanted.toLowerCase() === "latest")
    return board.trips.length ? board.trips.length - 1 : null;
  const index = board.trips.findIndex((trip) => trip.trip_id === wanted);
  return index === -1 ? null : index;
}

/**
 * The open task `wanted` names: exact text first, then a unique-enough substring.
 *
 * Substring as a fallback because the caller is a model writing prose ("the hotel") for a task the
 * board spells out in full ("Search and book the hotel"), and refusing that leaves the checklist
 * permanently stale for no good reason.
 */
function matchTask(tasks: readonly TripTask[], wanted: string): number | null {
  const lowered = wanted.trim().toLowerCase();
  if (!lowered) return null;
  const exact = tasks.findIndex((task) => !task.done && task.text.toLowerCase() === lowered);
  if (exact !== -1) return exact;
  const partial = tasks.findIndex(
    (task) => !task.done && task.text.toLowerCase().includes(lowered),
  );
  return partial === -1 ? null : partial;
}

/** The open tasks `names` resolve to, each at most once, and the names that resolved to none. */
function matchTasks(tasks: readonly TripTask[], names: readonly string[]) {
  // Matched against a local copy that is updated as we go, so two names cannot resolve to the
  // same open task and silently tick one thing twice.
  const local = [...tasks];
  const matched: number[] = [];
  const missed: string[] = [];
  for (const wanted of names) {
    const found = matchTask(local, wanted);
    if (found === null || matched.includes(found)) {
      missed.push(wanted);
      continue;
    }
    matched.push(found);
    local[found] = { text: local[found]!.text, done: true };
  }
  return { matched, missed };
}

function reply(board: BoardRef, index: number, note: string): TripBoardResponse {
  const trip = board.current.trips[index]!;
  return {
    trip_id: trip.trip_id,
    status: trip.status,
    remaining: trip.remaining.filter((task) => !task.done).map((task) => task.text),
    note,
    board: render(board.current),
  };
}

/**
 * A miss is reported, never thrown: a wrong id is the caller's to correct, and a tool error would
 * end the turn over a bookkeeping slip.
 */
function noSuchTrip(board: BoardRef, tripId: string): TripBoardResponse {
  return {
    trip_id: tripId,
    status: "unknown",
    remaining: [],
    note: `No trip '${tripId}' on the board. Open one with open_trip, or pass "latest" for the most recent.`,
    board: render(board.current),
  };
}

// ---------------------------------------------------------------------------------------------
// The tools
// ---------------------------------------------------------------------------------------------
//
// All inherently safe: writing to the agent's own notes cannot book anything, spend anything, or
// touch the outside world. The travel tools stay gated; see the approval policy in
// conversational-workflow.ts.

const board = { board: injected<BoardRef>() };

export const openTrip = tool({
  name: "open_trip",
  description: `Put a new trip on the board and return its \`trip_id\`.

Call this as soon as you know what trip you are working on — before searching, not
after booking. \`description\` is one line a person would recognize ("SFO to JFK,
Jul 1-5"); \`traveler\` is who it is for. \`tasks\` is the starting checklist; leave it
empty for the standard one, or pass your own plan for this trip.

Everything else on this board is addressed by the \`trip_id\` this returns.`,
  inputSchema: z.object({ request: OpenTripRequest }),
  outputSchema: TripBoardResponse,
  inherentlySafe: true,
  injected: board,
  execute: async ({ request }, { board }) => {
    const tripId = `TRIP-${uuid4().slice(0, 6).toUpperCase()}`;
    const given = request.tasks.map((text) => text.trim()).filter(Boolean);
    const tasks = given.length ? given : DEFAULT_TASKS;
    board.mutate((draft) => {
      draft.trips.push({
        trip_id: tripId,
        description: request.description,
        traveler: request.traveler,
        status: "collecting",
        flights: [],
        hotels: [],
        remaining: tasks.map((text) => ({ text, done: false })),
      });
    });
    return reply(board, board.current.trips.length - 1, `Opened ${tripId}.`);
  },
});

export const addTripTasks = tool({
  name: "add_trip_tasks",
  description: `Add items to a trip's checklist — things you have discovered it still needs.

Use it when the trip turns out to want something the plan did not have (a second
hotel, a visa check, a seat request). Pass \`"latest"\` as \`trip_id\` for the most
recently opened trip.`,
  inputSchema: z.object({ request: TripTasksRequest }),
  outputSchema: TripBoardResponse,
  inherentlySafe: true,
  injected: board,
  execute: async ({ request }, { board }) => {
    const index = findTrip(board.current, request.trip_id);
    if (index === null) return noSuchTrip(board, request.trip_id);
    const wanted = request.tasks.map((text) => text.trim()).filter(Boolean);
    board.mutate((draft) => {
      for (const text of wanted) draft.trips[index]!.remaining.push({ text, done: false });
    });
    return reply(board, index, `Added ${wanted.length} task(s).`);
  },
});

export const completeTripTasks = tool({
  name: "complete_trip_tasks",
  description: `Tick items off a trip's checklist.

Name each task by its text — the exact wording, or a distinctive part of it. Do this
as soon as the work is actually done, not at the end of the conversation: the board is
what the user is watching, and a checklist updated in one batch at the end was never a
checklist.

\`record_booking\` already ticks off what a booking finishes, so you do not need this for
those.`,
  inputSchema: z.object({ request: TripTasksRequest }),
  outputSchema: TripBoardResponse,
  inherentlySafe: true,
  injected: board,
  execute: async ({ request }, { board }) => {
    const index = findTrip(board.current, request.trip_id);
    if (index === null) return noSuchTrip(board, request.trip_id);
    const { matched, missed } = matchTasks(board.current.trips[index]!.remaining, request.tasks);
    // One block, so ticking three tasks is one version carrying three ops.
    board.mutate((draft) => {
      for (const position of matched) draft.trips[index]!.remaining[position]!.done = true;
    });
    let note = `Completed ${matched.length} task(s).`;
    if (missed.length) note += ` No open task matched: ${missed.map((t) => `'${t}'`).join(", ")}.`;
    return reply(board, index, note);
  },
});

export const recordBooking = tool({
  name: "record_booking",
  description: `Record a flight or hotel you have just booked, and tick off what it finishes.

Call it immediately after \`book_flight\` / \`book_hotel\` returns, with the confirmation
code that call gave you — never one you made up. \`detail\` is the line a person would
want to read back: airline, flight number and times, or hotel name and nights.

\`completes\` names the checklist items this booking satisfies; they are ticked in the
same commit as the booking, so the board never shows a trip with the flight booked and
"book the flight" still outstanding.`,
  inputSchema: z.object({ request: RecordBookingRequest }),
  outputSchema: TripBoardResponse,
  inherentlySafe: true,
  injected: board,
  execute: async ({ request }, { board }) => {
    const index = findTrip(board.current, request.trip_id);
    if (index === null) return noSuchTrip(board, request.trip_id);
    const { matched } = matchTasks(board.current.trips[index]!.remaining, request.completes);
    const booking: Booking = {
      kind: request.kind,
      confirmation_code: request.confirmation_code,
      detail: request.detail,
      price_usd: request.price_usd,
    };
    // The booking and the tasks it closes land in ONE mutate(), so a consumer of the state stream
    // never observes a moment where they disagree.
    board.mutate((draft) => {
      const trip = draft.trips[index]!;
      if (request.kind === "flight") trip.flights.push(booking);
      else trip.hotels.push(booking);
      for (const position of matched) trip.remaining[position]!.done = true;
    });
    return reply(
      board,
      index,
      `Recorded ${request.kind} ${request.confirmation_code}` +
        (matched.length ? `; completed ${matched.length} task(s).` : "."),
    );
  },
});

export const setTripStatus = tool({
  name: "set_trip_status",
  description: `Move a trip to \`booked\` once everything is arranged, or \`cancelled\` if it is off.

Leave it \`collecting\` while there is anything left to do.`,
  inputSchema: z.object({ request: TripStatusRequest }),
  outputSchema: TripBoardResponse,
  inherentlySafe: true,
  injected: board,
  execute: async ({ request }, { board }) => {
    const index = findTrip(board.current, request.trip_id);
    if (index === null) return noSuchTrip(board, request.trip_id);
    board.mutate((draft) => {
      draft.trips[index]!.status = request.status;
    });
    return reply(board, index, `Status is now ${request.status}.`);
  },
});

export const readTripBoard = tool({
  name: "read_trip_board",
  description: `Return the whole board: every trip, its bookings, and what each still needs.

Takes no arguments. The board is durable workflow state, so this is how you recall
where you left off at the start of a follow-up request.`,
  inputSchema: z.object({}),
  outputSchema: z.string(),
  inherentlySafe: true,
  injected: board,
  execute: async (_, { board }) => render(board.current),
});

// Handed to `codeModeTool` alongside the travel tools, and named here so the approval policy can
// allow exactly these by name without loosening anything else.
export const BOARD_TOOLS = [
  openTrip,
  addTripTasks,
  completeTripTasks,
  recordBooking,
  setTripStatus,
  readTripBoard,
];

export const BOARD_TOOL_NAMES = BOARD_TOOLS.map((t) => t.name);
