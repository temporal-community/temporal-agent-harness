# Wiki keeper — a callback-tools example

A conversational agent that organizes a tree of Markdown files into a personal wiki. You tell it
things ("remember that the staging DB URL is …", "what do I have on Temporal?", "tidy up my
recipes") and an LLM decides how to keep your notes: when to create a new file, append to an
existing one, delete an obsolete one, or restructure.

The point of this example is **callback tools**. The agent is a durable Temporal workflow that
could be running anywhere (picture a cloud worker) and has **no access to your disk**. Its six
filesystem tools — `ls`, `tree`, `read_file`, `write_file`, `delete_file`, `grep` — are
`@agent.callback_tool_defn` tools: each call **pauses inside the workflow** and publishes a
`callback_requested` event. A thin client running **on your machine** executes the operation
against a local wiki directory and posts the result back, and the agent resumes. The agent never
touches a filesystem; you opt into exactly which side effects run on your laptop by which tools
your client implements.

```
  you (terminal client)                 server (:8000)              WikiAgent workflow (worker)
  ─────────────────────                 ──────────────              ───────────────────────────
  "remember X"  ──POST /api/chat──────────────────────────────────▶ ask(): model calls write_file
                                                                      │  tool pauses, publishes
                ◀───────── callback_requested (SSE) ─────────────────┘  callback_requested
  write_file() on local disk
                ──POST /api/callback-result──────────────────────────▶ result validated, tool
                                                                        returns, model replies
                ◀───────── reply_delta … (SSE) ──────────────────────
```

Nothing "advertises" which tools the client implements: if the agent calls a tool the client
doesn't know, the call simply sits unresolved (visible under `agent_status.pending_callbacks`)
until something fulfills it. That's fine for this demo.

## What's here

| File | Role |
| --- | --- |
| `tools.py` | The six callback tools — declarations only (`...` bodies); the client supplies the impl. |
| `workflow.py` | `WikiAgent`: a Gemini tool-calling loop that converses and calls the tools. |
| `worker.py` | Hosts `WikiAgent` (no tool activities — callback tools are inline). |
| `client.py` | The thin terminal client: chats, and executes callback tools against a local wiki dir. |
| `agents.toml` | This example's registry (just the `WikiAgent`). |
| `justfile` | Recipes for the local stack. |

The web server and session-manager worker are **shared across examples** — `just server` points the
shared `examples/app.py` at this example's `agents.toml`, and `just session-manager` runs the shared
`examples/session_manager_worker.py`. The `POST /api/callback-result` route the client uses lives in
the packaged server.

## Setup

This example reuses the **shared env at the repo root**, `.env.local` — one env file for all
examples. If you haven't created it yet:

```bash
cp .env.example .env.local     # run from the repo root; then set GEMINI_API_KEY
```

## Run

Each in its own terminal, from **this directory** (`examples/callback_tools/wiki_agent`). All
recipes read the shared repo-root `.env.local`.

```bash
just temporal          # 1. local Temporal dev server (skip if you bring your own)
just session-manager   # 2. session-manager worker
just server            # 3. FastAPI API on http://localhost:8000
just worker            # 4. the WikiAgent worker
just client            # 5. the terminal client (writes to ./wiki by default)
```

Point the client at any directory you like: `just client --wiki-dir ~/notes`.

Then chat:

```
you> remember that our staging DB is at db.staging.internal:5432, user "app"
  · tree(path='.') → 1 line(s)
  · write_file(path='infra/staging.md', content=<118 chars>) → 44 characters ...
wiki> Noted — I created infra/staging.md with the staging database connection details.

you> actually add that the read replica is db-ro.staging.internal
  · read_file(path='infra/staging.md') → 118 chars
  · write_file(path='infra/staging.md', content=<181 chars>) → wrote 181 characters to infra/staging.md
wiki> Added the read replica to infra/staging.md.
```

Watch `./wiki` fill up with real Markdown files as you talk.

## How it maps to the harness

- The tools are ordinary harness tools, so they go through the **same tool path** as any other:
  `run_tool` → the tool-approval policy → `tool_start` → (here) `callback_requested` → the client's
  result → `tool_end`. This agent runs `dangerously_skip_all` approvals because *you* are the one
  executing each call; set `AgentConfig.approval_policy` to gate them and they'd require approval
  first, exactly like every other tool.
- The client uses only the packaged HTTP server: `POST /api/chat` (send + stream a turn),
  `POST /api/callback-result` (the one route this example motivated), and `GET /api/status`.
- A callback result is validated against the tool's declared return type before it resolves the
  call, and the submission is idempotent — see `temporal_agent_harness/harness` and the tests in
  `tests/harness/test_callback_tools.py`.
