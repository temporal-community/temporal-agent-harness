# ReAct agent

A conversational agent on the harness with the **OpenAI Agents SDK** in the **ReAct pattern**:
it *reasons*, then *acts* by calling a tool, and loops on the result until it can answer. It's the
harness form of workshop **demo2 + demo3** (OpenAI Agents SDK + Temporal integration, then MCP):
the Agents SDK drives the reason-act loop, Temporal makes it durable, and the harness standardizes
it behind the same contract, streaming path, and approval policy as every other harness agent.

## What it does

Ask it a question and it chains tools to find the answer:

- **Weather by city** — `get_coordinates(city)` → `get_weather(lat, lon)`
- **Weather where you are** — `get_ip_address()` → `get_location_info(ip)` → `get_weather(lat, lon)`
- **Formula 1 data** — tools from an external **F1 MCP server** (schedules, results, drivers, …)
- **Ask you a question** — when a request is ambiguous, it calls `ask_user` and waits for your answer

| Tool | Source | Purpose | Approval |
|------|--------|---------|----------|
| `get_ip_address` | icanhazip.com | The caller's public IP | auto (`inherently_safe`) |
| `get_location_info` | ip-api.com | City / country / lat-lon for an IP | auto (`inherently_safe`) |
| `get_coordinates` | Open-Meteo Geocoding | Lat-lon for a city name | **gated** |
| `get_weather` | Open-Meteo Forecast | Current temperature (°F), weather code, wind speed | **gated** |
| F1 tools | `f1-data` MCP server | Formula 1 schedules, results, and driver/constructor data | not gateable |
| `ask_user` | the human (callback tool) | Ask the user a clarifying question and use their answer | auto (`inherently_safe`) |

## What it demonstrates

- **Human-in-the-loop, the *participation* form — `ask_user`.** This is the mechanism the sample
  exists to show: the agent asks *you* a question mid-turn and uses your answer to keep going.
  `ask_user` is an `@agent.callback_tool_defn` tool — it has **no server-side body**. When the model
  calls it, the harness publishes a `callback_requested` event and **parks the turn in-workflow** on
  a `wait_condition` — durably, and never inside an activity, so no activity timeout is consumed.
  (`callback_tool_defn` accepts a `timeout=`; `ask_user` declares none, so it waits indefinitely.)
  An external client returns the answer via the `provide_callback_result` update — over HTTP
  at `POST /api/callback-result` — that answer becomes the tool's result, and the **same turn**
  resumes from exactly where it stopped. Fulfillment is decoupled from any particular UI: the
  terminal `client.py` here is one fulfiller, and anything that can call the endpoint is another.
- **Human-in-the-loop, the *authorization* form — tool approvals.** A separate feature that also
  involves a human. The agent's `ToolApprovalPolicy` is `allow_inherently_safe()`, so a tool that
  declared `inherently_safe=True` runs unattended and **everything else is gated on a human decision
  before it executes**. Here `get_ip_address` and `get_location_info` run freely, while
  `get_coordinates` and `get_weather` each wait for approval. The harness publishes
  `tool_approval_requested` and parks the call in-workflow — with **no** timeout, so it waits
  indefinitely — until a `tool_approval` update resolves it (`POST /api/approve`, with an optional
  `remember` to stop being asked about that tool). A denial never executes the tool: the model gets
  back `Tool 'x' failed: tool 'x' was not approved: …` and the loop carries on. Closing a session
  auto-denies anything still pending.
- **The two are independent.** They are unrelated features that happen to both involve a human, and
  neither is built on the other — different events (`callback_requested` vs
  `tool_approval_requested`), different Temporal updates (`provide_callback_result` vs
  `tool_approval`), different endpoints (`/api/callback-result` vs `/api/approve`), different
  harness code paths, and different waiting semantics (`callback_tool_defn` accepts a `timeout=`;
  the approval gate has none at all).
  Approvals play no part in `ask_user`. The one incidental point of contact is that *every* tool
  funnels through `run_tool`, so the approval policy is consulted for `ask_user` too — it is marked
  `inherently_safe=True`, which is what keeps *asking a question* from itself needing approval. The
  system prompt draws the same line for the model: "Do not ask permission to leverage your tools —
  that is handled by the harness you are running on." Asking for **information** is the model's job;
  asking for **authorization** is the harness's.
  - Approvals are deliberately **not** part of the agent's discoverable message contract
    (`tool_approval` is excluded from `accepted_message_types`), so a parent agent driving this one
    through its front door cannot approve its child's gated calls. Approvals come from a human,
    out of band.
- **A real ReAct loop.** The model reasons about the question, calls a tool, reads the result, and
  decides the next action — chaining several real API calls (and picking the right chain) until it
  can answer, all driven by the SDK's `Runner`.
- **Sample owned, activity-backed tools.** The weather/geo/IP tools are durable
  `@agent.activity_tool_defn`s (they do network I/O, so they run as Temporal activities, never
  inline), adapted onto the SDK with `as_openai_agent_tools(...)`. Every call flows through the
  harness's `run_tool`, so the harness keeps the approval policy and each tool's `tool_start` /
  `tool_end` / `tool_error` events. That is what made turning approvals on **a policy change, not a
  rewiring**: two `inherently_safe` flags and one line in `@workflow.init`. No tool was otherwise
  touched.
- **A durable MCP server.** The F1 tools come from an external MCP server registered on the worker
  with `StatelessMCPServerProvider` and referenced in the workflow with
  `stateless_mcp_server("f1-data")`. Each MCP `list_tools` / `call_tool` runs as a Temporal
  activity — durable, retryable, and visible in the Temporal Web UI.
  - **Caveat — MCP tools bypass the harness.** MCP calls do **not** go through `run_tool`, so they
    do not appear as harness tool cards on the turn stream and are **not** approval-gateable — the
    F1 tools run unattended whatever the policy says. The harness-wrapped weather tools show full
    lifecycle *and* honour the gate. That contrast is the harness boundary, now visible in a single
    session.
- **Streaming is a toggle.** By default (`REACT_AGENT_STREAM` unset/`1`) the turn runs
  `Runner.run_streamed(...)`, so model calls route through the streaming activity and the harness
  observer translates raw OpenAI events into the live turn stream (`model_interaction_started` →
  `reply_delta` … `tool_requested` … `tool_start` / `tool_end` → `model_interaction_ended`). Set
  `REACT_AGENT_STREAM=0` (in the **worker's** environment) and the turn runs `Runner.run(...)`
  instead: it completes and returns one reply. Tool cards, the `ask_user` flow, and the final reply
  still appear on the turn stream; token-by-token `reply_delta` and the `model_interaction_*`
  brackets do not. (The terminal client renders either mode.)

## Layout

| File | Role |
|---|---|
| `workflow.py` | `ReactAgent` — the harness agent; one `ask` handler, local tools + `ask_user` adapted onto the SDK plus the F1 MCP server, driven by `Runner.run_streamed`. |
| `tool_activities.py` | The four location/weather tools as `@agent.activity_tool_defn` activities (httpx), plus `ALL_TOOLS` / `ALL_ACTIVITIES`. |
| `human_tools.py` | The `ask_user` human-in-the-loop **callback tool** (`@agent.callback_tool_defn`), plus `HUMAN_TOOLS`. No activity body — fulfilled by a client. |
| `worker.py` | Worker hosting the workflow + the four tool activities; registers the F1 MCP provider and wires the plugin for the harness streaming seam. |
| `client.py` | A terminal client: a session picker that shows which sessions are **waiting on an `ask_user`**, then lets you answer open questions, chat, or create a session — all over HTTP. |
| `agents.toml` | Registry entry that makes this agent selectable in the shared web UI. |

The agent is driven by the shared example stack — the packaged `SessionManagerWorkflow` worker plus
the FastAPI app and web UI (`examples/app.py`); registering it in `agents.toml` is all that takes.
Unlike the simpler examples, it **also ships a terminal `client.py`**.

### Two clients, deliberately different

`client.py` is a **stop-gap until the packaged web UI supports `ask_user`** — not a second full
client. Capability parity between the terminal and the GUI is explicitly **not** a goal; each covers
the half it needs to.

| | answers `ask_user` | approves gated calls |
|---|---|---|
| terminal `client.py` | ✅ | ❌ |
| packaged web UI | ❌ | ✅ (Approve / Approve and remember / Reject) |

In practice: exercise `ask_user` from the terminal client, and approve gated tools from the web UI.
Note the consequence — a turn started in the terminal that reaches `get_coordinates` or
`get_weather` parks on an approval the terminal client does not render, and the picker's ⏳ column
counts pending `ask_user` callbacks only, so an approval-parked session shows `—` there.

## Run it

Prereqs, from the repo root:

1. `cp .env.example .env.local` and set `OPENAI_API_KEY` (and your Temporal connection profile).
2. Install the **F1 MCP server** locally (see below) and point the worker at it via
   `F1_MCP_SERVER_HOME`.

### The F1 MCP server

An external dependency, not bundled here — this example is the harness port of the workshop's
[demo3-mcp](https://github.com/temporal-community/ai-agents-workshop-python/blob/main/demo3-mcp/README.md),
and the F1 tools come from [`rakeshgangwar/f1-mcp-server`](https://github.com/rakeshgangwar/f1-mcp-server).
It's a Node.js (TypeScript) MCP server that shells out to `python3` for FastF1 data, so it needs
both a built Node entrypoint and a Python venv. Prereqs: Node.js 18+, Python 3.10+, `uv`.

```sh
git clone https://github.com/rakeshgangwar/f1-mcp-server.git
cd f1-mcp-server
npm install && npm run build          # produces build/index.js

uv venv                                # the Python side FastF1 shells into
source .venv/bin/activate
uv pip install fastf1 pandas numpy
deactivate
```

Then point the worker at that checkout:

```sh
export F1_MCP_SERVER_HOME=/absolute/path/to/f1-mcp-server   # or set it in the repo-root .env.local
```

The worker (`worker.py`) launches it per the workshop:
`bash -c "source $F1_MCP_SERVER_HOME/.venv/bin/activate && node $F1_MCP_SERVER_HOME/build/index.js"`.
`F1_MCP_SERVER_HOME` defaults to `~/Projects/Temporal/AI/MCP/f1-mcp-server` if unset. Verify the
exact steps against the workshop README linked above.

Then, each in its own terminal:

```sh
just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. builds the Svelte UI, then serves API + UI on http://localhost:8000
just worker            # 4. the agent worker
just client            # 5. terminal client that chats + answers ask_user
```

`just server` builds the web UI first, so it needs `pnpm` on your PATH. (If the build reports
missing modules, run `just app-install` once. For UI hot-reloading, `just ui-dev` runs it on Vite
with `/api` proxied to the server on :8000.)

**Driving the two HITL forms.** They live in different clients (see *Two clients, deliberately
different* above):

- **`ask_user` → the terminal client** (`just client`). This is the participation demo. The **web
  UI cannot answer `ask_user`** — it has no fulfillment affordance.
- **Tool approvals → the web UI** (http://localhost:8000, pick **ReAct Agent**). A pending call
  appears in the chat panel's approval tray with **Approve**, **Approve and remember**, and
  **Reject**. The web UI is also the better view of the tool-chaining demo: each call appears live
  and the reply streams in token by token, weather tools show as harness tool cards, and F1 MCP
  calls appear as activities in the Temporal Web UI (see the caveat above).

**The terminal client** opens a **session picker** that lists the ReAct agent's open sessions and
marks which are waiting on an `ask_user` (⏳). From it you can:

- **Create a new session** (`n`) and chat. Try *"What's the weather?"* — ambiguous, so the model
  calls `ask_user`; the client prompts you and your answer (e.g. *"Tokyo"*) flows back into the same
  turn. *"When is the next Formula 1 race?"* exercises the F1 MCP tools, which are never gated.
- **Answer a waiting session** — including one where the question was raised from the *web UI*
  (which can't answer it): pick the ⏳ session, its open questions are listed, and your answers are
  submitted; the client then shows the agent's continued reply.
- **`--session <id>`** opens a session directly, surfacing any already-pending question.

Inside a session, client-local navigation is keyed off `:` (not `/`, which is reserved for the
harness's own slash commands): `:questions` re-checks for open questions, `:sessions` returns to the
picker, and `:quit` exits.

Without `just`, the equivalent commands (from the repo root):

```sh
uv run --group examples python -m examples.session_manager_worker
uv run --group examples python -m examples.app examples/react_agent/agents.toml --host 0.0.0.0 --port 8000
uv run --group examples python -m examples.react_agent.worker
uv run --group examples python -m examples.react_agent.client
```
