# Auto mode example

The [Monty travel agent](../monty/README.md), with **auto mode** switched on: before a gated
tool call reaches you, Jev judges it against the operator's rules and approves it, denies it,
or leaves it for you. Anything Jev doesn't approve comes to you exactly as it would in the
Monty example, so auto mode only ever reduces how many approvals you see.

It is the same agent, with the same tools, the same Code Mode script tool and the same trip
board. This directory adds only the workflow (`workflow.py`, a copy of Monty's
`conversational_workflow.py`) and a worker that needs a second key.

## The three parts

`workflow.py` wires all three into the `AgentWorkflowRunner`:

- **The switch** — `ToolApprovalPolicy.auto_mode(pre_approved_tools=BOARD_TOOL_NAMES)`. The
  trip-board tools are pre-approved *above* auto mode, so Jev never spends a call on them.
- **The rules** — `_AUTO_APPROVAL_CRITERIA`, named criteria sets (`read_only`,
  `books_travel`, `run_travel_code`, and a `cautious` catch-all), each written as plain-language
  `approve_when` / `deny_when` / `escalate_when` rules.
- **The mechanism** — `agent.jev_evaluator()`.

Monty's travel tools are reused **unchanged**. They don't name a criteria set themselves, so
the rulebook assigns each one by name in `AutoApprovalCriteria.tools`. This is the same
mechanism that brings tools you didn't write, like an MCP server's, under auto mode. A tool
you *do* write can name its own default set instead, with
`@agent.activity_tool_defn(auto_approval_criteria="books_travel")`.

## What to watch in the console

- **Searches run without asking.** `search_flights` / `search_hotels` / `get_trip_summary`
  are `read_only`, which approves calls that only search or summarize.
- **Bookings still come to you.** `books_travel` escalates anything that actually books, so
  `book_flight` / `book_hotel` land on the approval gate as usual. That set also *denies*
  outright a booking for a traveller the user never asked to book for.
- **Every judgment is on the stream.** Each evaluation is bracketed by
  `auto_approval_evaluation_started` → `auto_approval_evaluation_ended` (or `_error` /
  `_superseded` if you answer first). The ended event carries Jev's verdict, its confidence and
  full distribution, the irreversibility judgment, the thresholds applied, and which criteria
  set decided it.
- **You can take it back.** `set_approval_policy` with the `human_approval_only` posture sends
  every gated call straight to you again, and `auto_mode` turns the evaluator back on.

## Setup

Copy the shared env template at the **repo root** and fill it in:

```bash
cp .env.example .env.local     # run from the repo root
```

- Set `GEMINI_API_KEY`, which the agent's conversation runs on.
- Set `TYPESAFE_API_KEY` (from <https://console.typesafe.ai/>) for the Jev evaluator.
  `TYPESAFE_AI_API_KEY` works too. The worker refuses to start without it. Without the key
  the agent would still run, but auto mode would never approve anything, which is harder to
  diagnose than a startup error. If you have no TypeSafe key, run the Monty example instead.
- `TEMPORAL_CONFIG_FILE` defaults to the repo's committed `temporal.local.toml` (a local dev
  server).

## Run

Each command in its own terminal, all from this directory:

```bash
just temporal          # 1. local Temporal dev server (skip if you bring your own)
just session-manager   # 2. session-manager worker
just server            # 3. FastAPI API + built Svelte UI  ->  http://localhost:8000
just worker            # 4. the auto-mode agent
```

Open <http://localhost:8000>, start a **Travel agent (Jev Auto mode)** session, and plan a trip.
