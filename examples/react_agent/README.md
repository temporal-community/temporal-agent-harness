# ReAct agent

A conversational agent on the harness with the **OpenAI Agents SDK** in the **ReAct pattern**:
it *reasons*, then *acts* by calling a tool, and loops on the result until it can answer. It's the
harness form of workshop **demo2 + demo3** (OpenAI Agents SDK + Temporal integration, then MCP):
the Agents SDK drives the reason-act loop, Temporal makes it durable, and the harness standardizes
it behind the same contract, streaming path, and (later) approval policy as every other harness
agent.

## What it does

Ask it a question and it chains tools to find the answer:

- **Weather by city** — `get_coordinates(city)` → `get_weather(lat, lon)`
- **Weather where you are** — `get_ip_address()` → `get_location_info(ip)` → `get_weather(lat, lon)`
- **Formula 1 data** — tools from an external **F1 MCP server** (schedules, results, drivers, …)

| Tool | Source | Purpose |
|------|--------|---------|
| `get_ip_address` | icanhazip.com | The caller's public IP |
| `get_location_info` | ip-api.com | City / country / lat-lon for an IP |
| `get_coordinates` | Open-Meteo Geocoding | Lat-lon for a city name |
| `get_weather` | Open-Meteo Forecast | Current temperature (°F), weather code, wind speed |
| F1 tools | `f1-data` MCP server | Formula 1 schedules, results, and driver/constructor data |

## What it demonstrates

- **A real ReAct loop.** The model reasons about the question, calls a tool, reads the result, and
  decides the next action — chaining several real API calls (and picking the right chain) until it
  can answer, all driven by the SDK's `Runner`.
- **Harness-owned, activity-backed tools.** The weather/geo/IP tools are durable
  `@agent.activity_tool_defn`s (they do network I/O, so they run as Temporal activities, never
  inline), adapted onto the SDK with `as_openai_agent_tools(...)`. Every call flows through the
  harness's `run_tool`, so the harness keeps the approval policy and each tool's `tool_start` /
  `tool_end` / `tool_error` events. Registering the tools through the harness now is what makes a
  future HITL demo **a policy change, not a rewiring**.
- **A durable MCP server.** The F1 tools come from an external MCP server registered on the worker
  with `StatelessMCPServerProvider` and referenced in the workflow with
  `stateless_mcp_server("f1-data")`. Each MCP `list_tools` / `call_tool` runs as a Temporal
  activity — durable, retryable, and visible in the Temporal Web UI.
  - **Caveat — MCP tools bypass the harness.** MCP calls do **not** go through `run_tool`, so they
    do not appear as harness tool cards on the turn stream and are **not** approval-gateable. The
    harness-wrapped weather tools still show full lifecycle. This illustrates the harness boundary.
- **Streaming, not blocking.** The turn runs `Runner.run_streamed(...)`, so model calls route
  through the streaming activity and the harness observer translates raw OpenAI events into the
  live turn stream (`model_interaction_started` → `reply_delta` … `tool_requested` … `tool_start` /
  `tool_end` … `model_interaction_ended`).
- **No approvals yet.** The approval policy is `dangerously_skip_all()` — tool calls run without a
  human gate. A future HITL demo flips this to gate the harness-wrapped tools.

## Layout

| File | Role |
|---|---|
| `workflow.py` | `ReactAgent` — the harness agent; one `ask` handler, local tools adapted onto the SDK plus the F1 MCP server, driven by `Runner.run_streamed`. |
| `tool_activities.py` | The four location/weather tools as `@agent.activity_tool_defn` activities (httpx), plus `ALL_TOOLS` / `ALL_ACTIVITIES`. |
| `worker.py` | Worker hosting the workflow + the four tool activities; registers the F1 MCP provider and wires the plugin for the harness streaming seam. |
| `agents.toml` | Registry entry that makes this agent selectable in the shared web UI. |

Like the other examples, there is **no per-example client**: it's driven by the shared example
stack — the packaged `SessionManagerWorkflow` worker plus the FastAPI app and web UI
(`examples/app.py`). Registering the agent in `agents.toml` is all it takes to make it driveable.

## Run it

Prereqs, from the repo root:

1. `cp .env.example .env.local` and set `OPENAI_API_KEY` (and your Temporal connection profile).
2. Install the **F1 MCP server** locally and point the worker at it. By default the worker looks in
   `~/Projects/Temporal/AI/MCP/f1-mcp-server`; override with `F1_MCP_SERVER_HOME`. The worker
   launches it with `node <home>/build/index.js` after activating that project's venv, so build it
   there first.

Then, each in its own terminal:

```sh
just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. builds the Svelte UI, then serves API + UI on http://localhost:8000
just worker            # 4. the agent worker
```

`just server` builds the web UI first, so it needs `pnpm` on your PATH. (If the build reports
missing modules, run `just app-install` once. For UI hot-reloading, `just ui-dev` runs it on Vite
with `/api` proxied to the server on :8000.)

Open http://localhost:8000, pick **ReAct Agent**, and try:

- *"What's the weather in Tokyo?"* (city → coordinates → weather)
- *"What's the weather where I am?"* (IP → location → weather)
- *"When is the next Formula 1 race?"* (F1 MCP tools)

Each tool call appears live as the model chains them, and the reply streams in token by token. The
weather tools show up as harness tool cards; the F1 MCP calls appear as activities in the Temporal
Web UI (see the caveat above).

Without `just`, the equivalent commands (from the repo root):

```sh
uv run --group examples python -m examples.session_manager_worker
uv run --group examples python -m examples.app examples/react_agent/agents.toml --host 0.0.0.0 --port 8000
uv run --group examples python -m examples.react_agent.worker
```
