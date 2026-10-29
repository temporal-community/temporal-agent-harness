# Monty travel-agent example (TypeScript)

The TypeScript twin of [the Python Monty example](../../../python/examples/monty/README.md),
written with `@temporalio/agent-harness` and run on a TypeScript worker. A "Monty" agent runs a
sandboxed Python script ([Monty](https://github.com/pydantic/monty)) whose only escape hatches are
host functions backed by durable Temporal activities (simulated flight/hotel search + booking).
Three agents ship here:

- **MontyDynamicAgentTs** — no model in the loop; each turn *is* a pre-written script that the
  agent runs in the sandbox.
- **MontyChatAgentTs** — conversational: you chat in plain text, a Gemini model writes its own
  script and runs it via a tool, then replies in prose.
- **MontyChatSubagentAgentTs** — same conversational experience, but it drives
  `MontyDynamicAgentTs` as a *subagent*.

The registry (`agents.toml`) lists the two conversational agents. Their workflow types carry a
`Ts` suffix and they run on their own task queue, `monty-dynamic-agent-ts`, so this example and the
Python one can be served side by side.

As in Python, the scripts are Python: Monty is the sandbox in both SDKs. In TypeScript each
sandbox step runs in an activity on the worker (Python steps it inside the workflow).
`AgentHarnessPlugin` registers those activities, as it does wherever `@pydantic/monty` is
installed.

## The trip board

All three agents keep a **trip board** — a running TODO list of the trips they are collecting,
each with a description, the flights and hotels booked so far, and what is still left to do. It
lives in `trip-board.ts` as [observable agent
state](../../../docs/design/observable-agent-state.md): the agent declares it with one entry,

```ts
export const MontyChatAgentTs = defineAgent({
  states: { trip_board: state(TripBoard) },
  // ...
});
```

and from then on every change publishes RFC 6902 JSON Patch ops on the agent's `turn_events`
stream. Open the **AGENT STATE** pane in the console (the `+` on the pane rail) to watch it: a
booked flight lands as `add /trips/0/flights/-` and the checklist item it closes ticks in the
same commit, ordered against the `tool_end` that produced it.

The board tools are ordinary host functions in the same sandbox as the travel tools, so one
script can book a flight and record it without a round trip through the model. They run INLINE
in the workflow rather than as activities (the board is the agent's own notes, not an action on
the world), and `MontyChatAgentTs` does not gate them on a human approval — a click between every
step and the board that is meant to be showing the steps would defeat it.

## Asking you mid-turn

Both conversational agents can ask you a question without ending their turn. `ask_user`
(`ask-user.ts`) is a **callback tool**: when the model calls it, the turn pauses and the console
shows the model's question with an answer box. Your answer goes back to the model as the tool's
result, and the turn carries on. The model decides when to ask and what — a traveler's name, a
date, which of the flights it found you want. Each call asks one question; to ask several
things, the model makes one call per question in the same reply, and you get a card for each.
Asking needs no approval; the agents allow `ask_user` by name.

## Files

| File | What it holds |
| --- | --- |
| `conversational-workflow.ts` | `MontyChatAgentTs`: the Gemini Interactions tool loop over a Code Mode tool and `ask_user` |
| `ask-user.ts` | `ask_user`, the callback tool both conversational agents ask you questions with |
| `conversational-subagent-workflow.ts` | `MontyChatSubagentAgentTs`: the same loop over the subagent toolset |
| `workflow.ts` | `MontyDynamicAgentTs`: runs the script it is sent |
| `activities.ts` | The travel tools as the workflows declare them |
| `activity-bodies.ts` | Their simulated implementations, which run on the worker |
| `trip-board.ts` | The observable trip board and its inline tools |
| `models.ts`, `travel-models.ts` | The message and request/response schemas |
| `agents.ts` | The module the worker bundles: every agent, under its workflow type |
| `worker.ts` | The worker |

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

Then open <http://localhost:8000> and pick **Monty (Conversational, TypeScript)**. Approve the
`run_travel_code` call to see the script the model wrote, and each search or booking it makes.
For frontend development, `just ui-dev` runs Vite at <http://127.0.0.1:5173> with `/api` proxied
to the same FastAPI server.

`just dev` runs the session manager, server and worker in one terminal against a running
Temporal. `just` lists every recipe; `just config` shows the Temporal connection currently in use.

## Test

```bash
bazel test //typescript/examples/monty/...
```

runs `MontyChatAgentTs` on a dev server against a scripted Gemini client (no network or API key):
the model runs a Code Mode script that opens a trip and searches flights, the script's result goes
back to the model, and its reply streams.
