# Hello-world Pydantic AI agent

The smallest interesting agent you can build on the harness with **Pydantic AI**: it chats in plain
text and can call one tool (`get_weather`). It's here to show the harness **streaming** path for the
Pydantic AI integration end to end — from the model call, through Pydantic AI's model activity, to
the live turn stream the web UI consumes.

Unlike the OpenAI and Gemini integrations (whose plugins are vendored into the harness so a streaming
seam could be added), Pydantic AI's Temporal plugin is used **unmodified** from
`pydantic_ai.durable_exec.temporal`: it already ships a first-class `event_stream_handler` hook that
runs inside the model-request activity on the live event stream, which is exactly the seam the
harness needs. The harness only supplies a thin glue module.

## What it demonstrates

- **Streaming via `event_stream_handler`.** The agent is built with
  `event_stream_handler=harness_event_stream_handler`. When you call `agent.run(...)`, Pydantic AI
  streams the model request into its `…__model_request_stream` activity and invokes the handler
  there on the live `AgentStreamEvent`s. The handler translates them into harness vocabulary —
  `model_interaction_started` → `reply_delta` / `thought_summary` … `tool_requested` …
  `model_interaction_ended` (with token usage).
- **Explicit per-run threading (no `self._runner` assumption).** The `TemporalAgent` is built once
  at module load (its activities are registered on the worker). Each turn threads the runner through
  `deps` — that's all you pass:
  ```python
  await agent.run(message.text, deps=HarnessDeps(runner=self._runner), message_history=self._history)
  ```
  `HarnessDeps` snapshots `harness_stream_context` from the runner for you. Both handles are needed
  but consumed in different places: the runner stays a live in-workflow reference the adapted tools
  read off `deps` (excluded from serialization), while the snapshotted stream context rides across
  into the model activity — where the handler runs and the runner can't follow.
- **Harness-owned tools.** `get_weather` is a normal `@agent.tool_defn`, adapted onto the SDK with
  `build_harness_toolset([...])`, which also returns the `tool_activity_config` that disables
  Pydantic AI's per-tool activity wrapper so the tool runs **in-workflow** — where the harness
  approval gate and `tool_start`/`tool_end` events live.

## Layout

| File | Role |
|---|---|
| `workflow.py` | `PydanticAIHelloAgent` — the harness agent; one `ask` handler, one `get_weather` tool, driven by `TemporalAgent.run` + the harness event-stream handler. |
| `worker.py` | Worker hosting the workflow; `PydanticAIPlugin` on the client, `AgentPlugin(agent)` on the worker. |
| `agents.toml` | Registry entry that makes this agent selectable in the shared web UI. |

There is **no per-example client**: like the other examples, this agent is driven by the shared
example stack — the packaged `SessionManagerWorkflow` worker plus the FastAPI app and web UI
(`examples/app.py`). Registering the agent in `agents.toml` is all it takes to make it driveable.

## Run it

Prereq: from the repo root, `cp .env.example .env.local` and set `OPENAI_API_KEY` (and your Temporal
connection profile). Then, each in its own terminal:

```sh
just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. builds the Svelte UI, then serves API + UI on http://localhost:8000
just worker            # 4. the agent worker
```

`just server` builds the web UI first, so it needs `pnpm` on your PATH. (If the build reports missing
modules, run `just app-install` once. For UI hot-reloading, `just ui-dev` runs it on Vite with
`/api` proxied to the server on :8000.)

Open http://localhost:8000, pick **Pydantic AI Hello**, and chat — e.g. *"What's the weather in
Paris?"*. The reply streams in token by token and the `get_weather` tool call appears live, each
event produced by the handler translating a raw Pydantic AI event in the model activity.

Without `just`, the equivalent commands (from the repo root):

```sh
uv run --group examples python -m examples.session_manager_worker
uv run --group examples python -m examples.app examples/pydantic_ai_hello/agents.toml --host 0.0.0.0 --port 8000
uv run --group examples python -m examples.pydantic_ai_hello.worker
```
