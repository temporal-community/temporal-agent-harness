# Coding agent — a callback-tools example (OpenCode front end)

A conversational **coding agent** that works on your project: ask it to explain code, fix a bug,
add a feature, write tests, or run a command, and an LLM reasons and calls `bash` / `read` /
`write` / `edit` / `grep` / `glob` to do it.

The point of this example is **callback tools + a real coding-TUI front end**. The agent is a
durable Temporal workflow that could be running anywhere (picture a cloud worker) and has **no
access to your disk**. Its tools are `@agent.callback_tool_defn` tools: each call **pauses inside
the workflow** and publishes a `callback_requested` event. A client on **your machine** executes
the operation against your local project and posts the result back, and the agent resumes.

Here that client is the **OpenCode shim** — the same process that lets the stock
[OpenCode](https://opencode.ai) TUI attach to it. So you chat in a polished terminal UI while a
durable workflow does the reasoning, and the shim runs the agent's tool calls on your laptop —
**after you approve each one**.

```
 OpenCode TUI ──opencode protocol──▶ shim (laptop, :4096) ──HTTP──▶ packaged server (:8000) ──▶ CodingAgent workflow
 (renders chat,                      ├─ IS the server the TUI attaches to                        (durable, cloud)
  approval prompts,                  └─ IS the callback client: runs bash/read/write/edit
  tool cards)                           on your disk, after you approve
                                     the Svelte UI (:8000) observes the SAME workflow ───────────┘
```

Two frontends watch the **same** durable workflow: the OpenCode TUI (via the shim) and the
packaged **Svelte UI** on :8000 (great for visibility/debuggability). The shim reaches the agent
through the packaged HTTP server, so every session goes through the session-manager and shows up
in the Svelte UI.

## Approvals

This agent runs real shell commands and edits real files, so **every mutating tool is gated**
(`ToolApprovalPolicy.allow_inherently_safe()`): `bash` / `write` / `edit` each become an OpenCode
permission prompt, and the tool only runs once you say yes (once / always / reject). The read-only
tools (`read` / `grep` / `glob`) and the plan tools (`todowrite` / `todoread`) are declared
`inherently_safe`, so they auto-approve — no prompt, and they run concurrently, so the "orient"
phase isn't throttled by approving one thing at a time.

## What's here

| File | Role |
| --- | --- |
| `tools.py` | The six callback tools — declarations only (`...` bodies); the shim supplies the impl. |
| `workflow.py` | `CodingAgent`: a Gemini tool-calling loop that converses and calls the tools. |
| `worker.py` | Hosts `CodingAgent` (no tool activities — callback tools are inline). |
| `agents.toml` | This example's registry (just the `CodingAgent`). |
| `opencode_shim/` | The OpenCode-protocol server. `harness_backend.py` fronts the workflow; `local_tools.py` executes the callback tools on your disk; `backend.py` defines the `AgentBackend` protocol + the `AgentTurn` seam. |
| `justfile` | Recipes for the local stack. |

The web server and session-manager worker are **shared across examples** — `just server` points
the shared `examples/app.py` at this example's `agents.toml`, and `just session-manager` runs the
shared `examples/session_manager_worker.py`.

## Setup

Reuses the **shared env at the repo root**, `.env.local`. If you haven't created it yet:

```bash
cp .env.example .env.local     # run from the repo root; then set GEMINI_API_KEY
```

You'll also need the [`opencode`](https://opencode.ai) CLI on your machine.

## Run

Each in its own terminal, from **this directory** (`examples/callback_tools/coding_agent`):

```bash
just temporal          # 1. local Temporal dev server (skip if you bring your own)
just session-manager   # 2. session-manager worker
just server            # 3. packaged FastAPI API + Svelte UI on http://localhost:8000
just worker            # 4. the CodingAgent worker
just serve ~/some/proj # 5. the OpenCode shim; pass the project the agent should work on
```

The project dir comes first; a relative path (e.g. `./TestProject`) resolves against the directory
you run `just` from, and it defaults to that directory if omitted.

Then attach the TUI (in a sixth terminal):

```bash
opencode attach http://127.0.0.1:4096
```

`attach` is an OpenCode subcommand taking the URL positionally. Do **not** use
`OPENCODE_API_URL=... opencode` or `opencode --attach ...` — neither attaches; they launch OpenCode
normally (its own server + bundled model), and the shim is never hit. Pin your opencode version —
this is OpenCode's internal protocol, not a formal standard, and it shifts between releases.

Chat with it (`add a test for the parser`, `why does main.py crash on empty input?`). Each `bash`
/ `read` / `write` / `edit` shows up as an OpenCode permission prompt first; approve it and the
tool runs on your machine, its result streams back, and the agent continues. Open
http://localhost:8000 to watch the same session in the Svelte UI.

## Tool rendering

The callback tools are declared snake_case in `tools.py` (idiomatic Python); the shim maps their
args to OpenCode's canonical camelCase (`filePath` / `oldString` / `newString`) so the TUI's
per-tool renderers light up. OpenCode reads each tool's *result* from a tool-specific `metadata`
key (not a generic output field), so the shim populates what each card needs: `bash` →
`metadata.output` + `exit`, `edit` → `metadata.diff`, `write` → `metadata.diagnostics` (its
presence makes the card show the written content), `grep` → `metadata.matches`, `glob` →
`metadata.count`. It also shows the tool card *before* the approval prompt so the prompt can
display the command/args, and renders `write` as an `edit` in the permission dialog (OpenCode has
no `write` prompt) so you see a diff of what will be written.

## More OpenCode capabilities

- **Reasoning / thinking** — the workflow enables Gemini thinking summaries
  (`generation_config.thinking_summaries`); the shim maps the harness `thought_summary` events to
  OpenCode's collapsible "thinking" block (a `reasoning` message part).
- **Todo list** — `todowrite`/`todoread` are *inline* tools (not callbacks): the plan is durable
  **workflow state** (`self._todos`), supplied to each call as an `Injected` sink, so it survives
  across turns. `todowrite` renders the checklist card and backs OpenCode's live todo panel
  (`/session/{id}/todo`); `todoread` lets the agent recall its plan. Both are `inherently_safe`, so
  the `allow_inherently_safe()` policy auto-approves them while every machine-touching tool stays
  gated.
- **Change review** — `/session/{id}/diff` returns the working-tree diff vs `HEAD` (the shim runs
  `git` in the project dir), so OpenCode's diff viewer shows everything the agent changed.
- **Abort** — aborting in the TUI signals the harness `close` (via `POST /api/sessions/{id}/close`
  on the packaged server), which really stops the durable agent; the next prompt starts a fresh
  session.

## How it maps to the harness

- The tools are ordinary harness tools, so they go through the **same tool path** as any other:
  `run_tool` → the tool-approval policy (here: gate everything) → `tool_start` →
  `callback_requested` → the shim's result → `tool_end`.
- The shim uses only the packaged HTTP server: `POST /api/sessions` (create the durable session
  via the session-manager), `POST /api/chat` (send + stream a turn), `POST /api/approve` (relay
  the OpenCode permission decision), `POST /api/callback-result` (return each tool's result), and
  `POST /api/sessions/{id}/close` (abort).
- A callback result is validated against the tool's declared return type before it resolves the
  call, and the submission is idempotent — see `temporal_agent_harness/harness` and the tests in
  `tests/harness/test_callback_tools.py`.

## Not yet wired

**Subagents.** The harness supports subagents (an agent driving child agents), which OpenCode
renders as a `task` tool with a nested child session. Surfacing them here means giving the agent a
subagent toolset and demultiplexing the merged event stream by `agent_id` in the shim — including
routing each subagent's `callback_requested` to the *child* workflow (or the subagent deadlocks).
It's a sizeable feature; not implemented yet.

Abort ends the whole durable session (the harness has no per-turn cancel); the next prompt starts
a fresh one.
