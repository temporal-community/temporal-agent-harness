# Tic-tac-toe (TypeSafe) example

A harness agent with **no LLM in the loop**. Every other example here puts a generative model
in a tool-calling loop; this one replaces the model call with a
[TypeSafe](https://docs.typesafe.ai) System One call — a fast, typed judgment (a probability
distribution, not text) that the workflow's own code turns into an action.

You play X (or let the agent open), sending a cell number per turn. On its turn the agent
asks TypeSafe's `jev` three questions **in one request** over the same board state:

| id        | Primitive | Question                                                            | Used for            |
| --------- | --------- | ------------------------------------------------------------------- | ------------------- |
| `move`    | Choice    | Which empty cell to take (the legal cells *are* the options)        | the move it makes   |
| `threat`  | Noul      | Can the opponent complete a line on their very next move?           | the reply's remark  |
| `outlook` | Score     | `losing` / `even` / `winning` for the agent, assuming good play      | the reply's remark  |

Both halves of the agent's turn are **tool calls** dispatched through `runner.run_tool`:
the TypeSafe request itself (`typesafe_system_one`, an activity-backed tool) and then the
action it decides (`place_mark`, an inline workflow tool that mutates the board). The `move`
answer is the agent's whole policy: the workflow reads `choice` and places it. The rules of
the game (legal moves, win detection, rendering) are code in `board.py`; the model only ever
chooses among cells the code offers, so an illegal move is impossible by construction.

## What to watch in the console

- **The TypeSafe exchange.** Because the model call is a tool, its `tool_start` carries the
  exact `state` + `questions` sent to TypeSafe and its `tool_end` the answers (choice,
  per-cell probabilities, confidence, token usage) — expand the `typesafe_system_one` activity
  row to read both. (The harness's model-interaction events carry only a model id and usage,
  which is why the call is modeled as a tool rather than a model span for now.)
- **The board as observable state.** The workflow declares `board.py`'s `Board` `HarnessState`
  as `board = agent.state(game.Board)`; open the **AGENT STATE** pane and each move lands as
  `replace /cells/4 "O"` + `add /moves/-` in one commit, ordered against the `tool_end` that
  made it. Your move is a patch too (applied in the handler); the agent's arrives via the tool.
- **The reply.** Shows the pick with its confidence and the top of the distribution, the
  `threat` probability, and the `outlook` score — the raw judgments, so you can see how well
  calibrated they are on a position you understand.

## Files

- `board.py` — the `Board` state, the rules, and the `place_mark` inline tool.
- `activities.py` — the TypeSafe tool: a generic `system_one(state, questions)` bridge over
  the TypeSafe Python SDK as an `@agent.activity_tool_defn`. The workflow sends questions as
  raw dicts, so the tool knows nothing about tic-tac-toe.
- `workflow.py` — `TicTacToeAgent`: the `new_game` / `play` handlers, the question design, and
  the two `run_tool` dispatches (judge, then act).
- `models.py` — the accepted messages and the activity's request/result shapes.
- `ui/` — a Svelte board for playing it, one component on `@temporal-agent-harness/svelte` (see below).
- `client_sdk/TicTacToeAgent.ts` — the agent's TypeScript types, generated from its Python
  models by `just codegen-client-sdk`.

## Setup

Copy the shared env template at the **repo root** and fill it in:

```bash
cp .env.example .env.local     # run from the repo root
```

- Set `TYPESAFE_API_KEY` (from <https://console.typesafe.ai/>). `TYPESAFE_AI_API_KEY` works too.
- `TEMPORAL_CONFIG_FILE` defaults to the repo's committed `temporal.local.toml` (a local dev
  server).

## Run

Each command in its own terminal, all from this directory:

```bash
just temporal          # 1. local Temporal dev server (skip if you bring your own)
just session-manager   # 2. session-manager worker
just server            # 3. FastAPI API + built Svelte UI  ->  http://localhost:8000
just worker            # 4. the tic-tac-toe agent
```

Open <http://localhost:8000>, start a **Tic-Tac-Toe (TypeSafe)** session, send `new_game`
(tick `agent_goes_first` to let it open), then `play` with a cell:

```
 1 | 2 | 3
---+---+---
 4 | 5 | 6
---+---+---
 7 | 8 | 9
```

## `ui/` — a board drawn from observable state

With `just server` and `just worker` up, `just play` serves the board on
http://127.0.0.1:5173. It talks to the dev server's `/api` at
`http://localhost:8000` (the server allows any origin). Start a game to create a session, then
play by clicking cells (or pressing 1–9); the session id stays in the URL, so a reload replays it.

The page is one Svelte component, `ui/App.svelte`, with no build step: the browser compiles it
(see [`ui/README.md`](ui/README.md)). The board is rendered **only** from the agent's observable
state: `agent.states.board`, typed as the generated `Board`, which `AgentSession` keeps current
from the session's `state_snapshot` and `state_patch` events. The page has no idea how a move is
applied. The ledger on the right reads the same session's raw frames — each patch's ops as they
land (`replace /cells/4 "O"`), the TypeSafe tool's request and answer, and the agent's reply — and
the last judgment's per-cell probabilities are washed onto the empty cells so you can see what the
model weighed. A gated tool call shows approve / deny buttons.
