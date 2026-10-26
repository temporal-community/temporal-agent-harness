// Monty dynamic agent: each turn runs a caller-supplied Python script, no model in the loop
// (Python's workflow.py).
//
// A deliberately minimal harness agent. Each turn carries an arbitrary Python *script* (RunScript)
// that runs in a sandbox whose only escape hatches are the travel host functions, each backed by a
// durable Temporal activity. There is no model in the loop: the "plan" arrives pre-written as the
// script.
//
// It's a thin consumer of the harness Code Mode feature: `codeModeTool` turns the travel activity
// tools into a single run-a-script tool, and the `run_script` handler dispatches the caller's
// script straight through it. The script calls the tools as async host functions; each host call
// runs as a durable, approval-gated activity, and independent calls can run concurrently via
// `asyncio.gather`.
//
// The conversational agents drive this one as a subagent (conversational-subagent-workflow.ts).

import { uuid4 } from "@temporalio/workflow";
import {
  MidTurn,
  TextReply,
  ToolApprovalPolicy,
  codeModeTool,
  defineAgent,
  handler,
  state,
} from "@temporalio/agent-harness";
import { ALL_TOOLS } from "./activities.ts";
import { RunScript } from "./models.ts";
import { BOARD_TOOLS, TripBoard } from "./trip-board.ts";

// The agent's own task queue, which the TypeScript worker polls; Python's agents poll
// "monty-dynamic-agent", so both languages' workers can run side by side.
export const TASK_QUEUE = "monty-dynamic-agent-ts";

const RUN_SCRIPT_DESCRIPTION = `Execute a Python \`\`script\`\` in a sandbox over the travel host functions, returning its
printed output + final value.

The sandbox has no filesystem, network, or arbitrary imports — only \`\`asyncio\`\` plus the
host functions below, each backed by a durable Temporal activity. Everything else is
ordinary in-sandbox Python. The value of the script's LAST EXPRESSION becomes the reply
(alongside anything \`\`print\`\`ed), so end the script with the value you want back.

The host functions are \`\`async\`\` — you MUST \`\`await\`\` them — so structure the script as::

    import asyncio
    async def main():
        ...                 # await host functions here
        return <final value>
    asyncio.run(main())

Run INDEPENDENT host calls concurrently with \`\`asyncio.gather\`\` (a gathered batch runs
concurrently, each call its own durable activity); only await sequentially when a later
call needs an earlier call's result. Each host function takes ONE argument — a dict
matching its request shape — and returns a dict; index results with normal Python
(\`\`resp["flights"][0]["price_usd"]\`\`). Scripts are statically type-checked against these
signatures before running, so a wrong argument shape or an unknown result key comes back
as an error to fix rather than a result (a bad script is normal input, not a failure).

Host functions (all \`\`async\`\`, all must be awaited):

  • \`\`search_flights({"origin": str, "destination": str, "date": str}) -> dict\`\`
        Returns \`\`{"flights": [flight, ...]}\`\`, each flight: \`\`flight_id\`\`, \`\`airline\`\`,
        \`\`departure_time\`\` ("HH:MM"), \`\`arrival_time\`\` ("HH:MM"), \`\`price_usd\`\` (float),
        \`\`stops\`\` (int).
  • \`\`search_hotels({"city": str, "check_in": str, "check_out": str}) -> dict\`\`
        Returns \`\`{"hotels": [hotel, ...]}\`\`, each hotel: \`\`hotel_id\`\`, \`\`name\`\`,
        \`\`star_rating\`\` (int), \`\`price_per_night_usd\`\` (float), \`\`neighborhood\`\` (str).
  • \`\`book_flight({"flight_id": str, "passenger_name": str}) -> dict\`\`
        Returns \`\`confirmation_code\`\`, \`\`flight_id\`\`, \`\`passenger_name\`\`, \`\`status\`\`.
  • \`\`book_hotel({"hotel_id": str, "guest_name": str}) -> dict\`\`
        Returns \`\`confirmation_code\`\`, \`\`hotel_id\`\`, \`\`guest_name\`\`, \`\`status\`\`.
  • \`\`get_trip_summary({"booking_refs": [str, ...]}) -> dict\`\`
        Returns \`\`{"summary": str}\`\` — a human-readable itinerary from confirmation codes.

The remaining host functions keep the agent's **trip board** — its running TODO list of
the trips it is collecting, which is observable state: every call publishes JSON Patch
ops on the turn stream, so a watching client sees the board change as the script runs.
Open a trip before searching, and record each booking as it happens rather than at the
end. All take one dict and return one dict (\`\`read_trip_board\`\` takes nothing and
returns a string); every board response carries \`\`trip_id\`\`, \`\`status\`\`, \`\`remaining\`\`,
\`\`note\`\` and a rendered \`\`board\`\`.

  • \`\`open_trip({"description": str, "traveler": str, "tasks": [str, ...]}) -> dict\`\`
        Puts a trip on the board and returns its \`\`trip_id\`\`. \`\`tasks\`\` is the starting
        checklist; omit or leave empty for the standard one.
  • \`\`record_booking({"trip_id": str, "kind": "flight"|"hotel", "confirmation_code": str,
    "detail": str, "price_usd": float, "completes": [str, ...]}) -> dict\`\`
        Records a booking and ticks off the checklist items it finishes, in one commit.
        Use a confirmation code a booking call actually returned.
  • \`\`complete_trip_tasks({"trip_id": str, "tasks": [str, ...]}) -> dict\`\`
        Ticks items off by their text (exact, or a distinctive part of it).
  • \`\`add_trip_tasks({"trip_id": str, "tasks": [str, ...]}) -> dict\`\`
        Adds items the trip turned out to need.
  • \`\`set_trip_status({"trip_id": str, "status": "collecting"|"booked"|"cancelled"}) -> dict\`\`
  • \`\`read_trip_board() -> str\`\`
        The whole board, rendered. Takes no arguments.

\`\`trip_id\`\` accepts \`\`"latest"\`\` for the most recently opened trip.

Example — open the board, search concurrently, book, record, summarize::

    import asyncio
    async def main():
        trip = await open_trip({
            "description": "SFO to JFK, Jul 1-5",
            "traveler": "Ada Lovelace",
        })
        flights, hotels = await asyncio.gather(
            search_flights({"origin": "SFO", "destination": "JFK", "date": "2026-07-01"}),
            search_hotels({"city": "New York", "check_in": "2026-07-01", "check_out": "2026-07-05"}),
        )
        cheapest = min(flights["flights"], key=lambda f: f["price_usd"])
        flight = await book_flight({"flight_id": cheapest["flight_id"], "passenger_name": "Ada Lovelace"})
        await record_booking({
            "trip_id": trip["trip_id"],
            "kind": "flight",
            "confirmation_code": flight["confirmation_code"],
            "detail": f"{cheapest['airline']} {cheapest['flight_id']} "
                      f"{cheapest['departure_time']}-{cheapest['arrival_time']}",
            "price_usd": cheapest["price_usd"],
            "completes": ["outbound flight"],
        })
        summary = await get_trip_summary({"booking_refs": [flight["confirmation_code"]]})
        return summary["summary"]
    asyncio.run(main())`;

export const MontyDynamicAgentTs = defineAgent({
  name: "MontyDynamicAgentTs",
  // The travel tools run inside a sandboxed simulation (no real-world side effects), so this
  // agent skips approvals by default. A caller can still tighten this per session via
  // AgentConfig.approval_policy.
  approvalPolicyDefault: ToolApprovalPolicy.dangerouslySkipAll(),
  // There is no model in this agent, so the script is what keeps the board current; see
  // trip-board.ts.
  states: { trip_board: state(TripBoard) },
  handlers: {
    // ENQUEUE: a parent drives this one turn at a time through the subagent FIFO gate, but a human
    // can also address this agent directly at the same time, so a second message should wait its
    // turn rather than be rejected.
    run_script: handler({
      description: RUN_SCRIPT_DESCRIPTION,
      input: RunScript,
      output: TextReply,
      midTurn: MidTurn.ENQUEUE,
    }),
  },
  create: ({ runner, state: { trip_board } }) => {
    // Code Mode over the travel tools plus the board tools: one tool that runs a script calling
    // them all as host functions. The run_script handler dispatches the caller's script straight
    // through it.
    const runCode = codeModeTool([...ALL_TOOLS, ...BOARD_TOOLS], {
      name: "run_travel_code",
      // Supplied per host call and hidden from the script: a script names a trip, never the
      // state the board lives in.
      injections: { board: trip_board },
    });
    return {
      run_script: async ({ script }) => ({
        text: await runner.runTool(uuid4(), runCode, { script }),
      }),
    };
  },
});
