# Monty travel-agent example

A Temporal-native agent example built on the harness. A "Monty" agent runs a sandboxed Python
script ([pydantic-monty](https://pypi.org/project/pydantic-monty/)) whose only escape hatches are
host functions backed by durable Temporal activities (simulated flight/hotel search + booking).
Three variants ship here:

- **MontyDynamicAgent** — no model in the loop; each turn *is* a pre-written script that the
  workflow runs in the sandbox.
- **MontyChatAgent** — conversational: you chat in plain text, a model writes its own script and
  runs it via a tool, then replies in prose.
- **MontyChatSubagentAgent** — same conversational experience, but it drives `MontyDynamicAgent`
  as a *subagent* (the first end-to-end exercise of the harness subagent toolset).

The agents are driven through the packaged session-manager workflow and shared
Svelte UI (`ui`). The Monty example owns the local app, registry, and recipes, so
the whole stack runs from this directory.

## The trip board

All three agents keep a **trip board** — a running TODO list of the trips they are collecting,
each with a description, the flights and hotels booked so far, and what is still left to do.
It lives in `trip_board.py` as [observable agent
state](../../docs/design/observable-agent-state.md): the workflow registers it with one call,

```python
self._board = self._runner.state("trip_board", trip_board.TripBoard())
```

and from then on every change publishes RFC 6902 JSON Patch ops on the agent's `turn_events`
stream. Open the **AGENT STATE** pane in the console (the `+` on the pane rail) to watch it: a
booked flight lands as `add /trips/0/flights/-` and the checklist item it closes ticks in the
same commit, ordered against the `tool_end` that produced it. Scrub the transport and the board
rewinds with you — it is a projection of the stream, not a live subscription.

The board tools are ordinary host functions in the same sandbox as the travel tools, so one
script can book a flight and record it without a round trip through the model. They run INLINE
in the workflow rather than as activities (the board is the agent's own notes, not an action on
the world), and they are the one thing `MontyChatAgent` does not gate on a human approval — a
click between every step and the board that is meant to be showing the steps would defeat it.

Easiest way to see it move, with no model and no API key — start a **Monty (Dynamic)** session
and send it this script (that agent's composer takes a script, not prose):

```python
import asyncio
async def main():
    trip = await open_trip({"description": "SFO to JFK, Jul 1-5", "traveler": "Ada Lovelace"})
    flights = await search_flights({"origin": "SFO", "destination": "JFK", "date": "2026-07-01"})
    cheapest = min(flights["flights"], key=lambda f: f["price_usd"])
    booked = await book_flight({"flight_id": cheapest["flight_id"], "passenger_name": "Ada Lovelace"})
    await record_booking({
        "trip_id": trip["trip_id"],
        "kind": "flight",
        "confirmation_code": booked["confirmation_code"],
        "detail": cheapest["airline"] + " " + cheapest["flight_id"],
        "price_usd": cheapest["price_usd"],
        "completes": ["outbound flight"],
    })
    return await read_trip_board()
asyncio.run(main())
```

## Setup

Copy the shared env template at the **repo root** and fill it in (it's gitignored, so your secrets
stay local). One `.env.local` at the repo root serves every example:

```bash
cp .env.example .env.local     # run from the repo root
```

- Set `GEMINI_API_KEY` — the conversational agents need it.
- `TEMPORAL_CONFIG_FILE` defaults to the repo's committed `temporal.local.toml` (a local dev
  server). To run against your own server or Temporal Cloud, create a private `temporal.toml`
  (gitignored) and point `TEMPORAL_CONFIG_FILE` at it (see `.env.example`).

## Run

Each command in its own terminal, all from this directory:

```bash
just temporal          # 1. local Temporal dev server (skip if you bring your own)
just session-manager   # 2. session-manager worker
just server            # 3. FastAPI API + built Svelte UI  ->  http://localhost:8000
just worker            # 4. the Monty agents
```

Then open <http://localhost:8000> and pick a Monty agent. For frontend
development, `just ui-dev` runs Vite at <http://127.0.0.1:5173> with `/api`
proxied to the same FastAPI server.

`just` lists every recipe; `just config` shows the Temporal connection currently in use.
