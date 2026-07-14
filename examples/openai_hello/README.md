# Hello-world OpenAI Agents agent

The smallest interesting agent you can build on the harness with the **OpenAI Agents SDK**: it
chats in plain text and can call one tool (`get_weather`). It's here to show the harness
**streaming** path for the OpenAI integration end to end — from the model call, through the
streaming activity, to the live turn stream the web UI consumes.

## What it demonstrates

- **Streaming, not blocking.** The turn runs `Runner.run_streamed(...)` (not `Runner.run`).
  Only the streamed path routes model calls through `invoke_model_activity_streaming`, and it's
  that activity that feeds each raw OpenAI event to the harness observer.
- **The observer seam.** The worker builds the plugin with the two harness hooks:
  ```python
  OpenAIAgentsPlugin(
      model_params=ModelActivityParameters(stream_to_provider=stream_to_provider),
      observer_factory=harness_observer_factory,
  )
  ```
  `stream_to_provider` resolves the in-flight turn's stream context ambiently off the running
  workflow (no runner threading); `harness_observer_factory` turns it into the observer that
  translates raw OpenAI events into harness vocabulary — `model_interaction_started` →
  `reply_delta` … `tool_requested` … `model_interaction_ended`. There are **no run hooks**:
  the model-interaction bracket comes from the observer.
- **Harness-owned tools.** `get_weather` is a normal `@agent.tool_defn`, adapted onto the SDK
  with `as_openai_agent_tool(...)`, so the harness keeps approval + `tool_start`/`tool_end`.

## Layout

| File | Role |
|---|---|
| `workflow.py` | `OpenAIHelloAgent` — the harness agent; one `ask` handler, one `get_weather` tool, driven by `Runner.run_streamed`. |
| `worker.py` | Worker hosting the workflow; wires the plugin for the harness streaming seam. |
| `agents.toml` | Registry entry that makes this agent selectable in the shared web UI. |

There is **no per-example client**: like the Monty example, this agent is driven by the shared
example stack — the packaged `SessionManagerWorkflow` worker plus the FastAPI app and web UI
(`examples/app.py`), which send messages and stream turns through the harness's built-in
`AgentClient`. Registering the agent in `agents.toml` is all it takes to make it driveable.

## Run it

Prereq: from the repo root, `cp .env.example .env.local` and set `OPENAI_API_KEY` (and your
Temporal connection profile). Then, each in its own terminal:

```sh
just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. builds the Svelte UI, then serves API + UI on http://localhost:8000
just worker            # 4. the agent worker
```

`just server` builds the web UI first, so it needs `pnpm` on your PATH. (If the build reports
missing modules, run `just app-install` once. For UI hot-reloading, `just ui-dev` runs it on Vite
with `/api` proxied to the server on :8000.)

Open http://localhost:8000, pick **OpenAI Hello**, and chat — e.g. *"What's the weather in
Paris?"*. The reply streams in token by token and the `get_weather` tool call appears live, each
event produced by the observer translating a raw OpenAI event in the streaming activity.

Without `just`, the equivalent commands (from the repo root):

```sh
uv run --group examples python -m examples.session_manager_worker
uv run --group examples python -m examples.app examples/openai_hello/agents.toml --host 0.0.0.0 --port 8000
uv run --group examples python -m examples.openai_hello.worker
```
