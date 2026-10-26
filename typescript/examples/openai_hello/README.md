# Hello-world OpenAI Agents agent (TypeScript)

The TypeScript twin of [`python/examples/openai_hello`](../../../python/examples/openai_hello):
the smallest interesting agent you can build on the harness with the **OpenAI Agents SDK**
(`@openai/agents-core`). It chats in plain text and can call one tool (`get_weather`). It's here to
show the harness **streaming** path for the OpenAI integration end to end — from the model call,
through the streaming activity, to the live turn stream the web UI consumes.

Its workflow type (`OpenAIHelloAgentTs`), registry key (`openai-hello-ts`) and task queue
(`openai-hello-ts`) differ from the Python example's, so the two can be served side by side.

## What it demonstrates

- **Streaming, not blocking.** The turn runs `runStreamed(runner, agent, input)` from
  `@temporalio/agent-harness/ai-sdks/openai-agents-harness`, which runs the agent on the
  `TemporalOpenAIRunner` with streaming on and routes every model call's events to the current
  turn. It's the streamed model activity that feeds each raw OpenAI event to the harness observer.
- **The observer seam.** The worker registers the harness's plugin:
  ```ts
  import { OpenAIAgentsPlugin } from "@temporalio/agent-harness-worker/ai-sdks/openai-agents";

  const worker = await Worker.create({
    connection,
    taskQueue,
    workflowsPath,
    plugins: [new OpenAIAgentsPlugin(), new AgentHarnessPlugin()],
  });
  ```
  Its streamed model activity builds the harness observer for each call, which translates raw
  OpenAI events into harness vocabulary — `model_interaction_started` → `reply_delta` …
  `tool_requested` … `model_interaction_ended`. There are **no run hooks**: the
  model-interaction bracket comes from the observer.
- **Harness-owned tools.** `get_weather` is a normal harness `tool(...)`, adapted onto the SDK with
  `asOpenAIAgentTool(runner, getWeather)`, so the harness keeps approval + `tool_start`/`tool_end`.

## Layout

| File | Role |
|---|---|
| `agents.ts` | `OpenAIHelloAgentTs` — the harness agent; one `ask` handler, one `get_weather` tool, driven by `runStreamed`. |
| `worker.ts` | A plain Temporal worker hosting the agent, with the OpenAI plugin and then `AgentHarnessPlugin` for the harness's own converter and activities. |
| `agents.toml`, `OpenAIHelloAgentTs.schema.json` | Registry entry that makes this agent selectable in the shared web UI, and the agent's schema it points at; generated from `BUILD.bazel` (`bazel run :registry_update`). |
| `agents.test.ts` | The agent on a dev server against a scripted model (no network): a weather question gets a `get_weather` call and a streamed reply. |

There is **no per-example client**: this agent is driven by the shared example stack — the packaged
`SessionManagerWorkflow` worker plus the FastAPI app and web UI (`temporal-agent-harness serve`),
which send messages and stream turns through the harness's built-in `AgentClient`. Both come from
the Python package; only the agent and its worker are TypeScript.

## Run it

Prereq: from the repo root, `cp .env.example .env.local` and set `OPENAI_API_KEY` (and your
Temporal connection profile). Then, from this directory, each in its own terminal:

```sh
just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. builds the Svelte UI, then serves API + UI on http://localhost:8000
just worker            # 4. the agent worker
```

(If the UI build reports missing modules, run `just app-install` once. For UI hot-reloading,
`just ui-dev` runs it on Vite with `/api` proxied to the server on :8000.)

Open http://localhost:8000, pick **OpenAI Hello (TypeScript)**, and chat — e.g. *"What's the
weather in Paris?"*. The reply streams in token by token and the `get_weather` tool call appears
live, each event produced by the observer translating a raw OpenAI event in the streaming activity.

Without `just`, the equivalent commands (from the repo root, with `.env.local`'s variables
exported):

```sh
bazel run //python/temporal_agent_harness/web:cli -- session-manager
bazel run //python/temporal_agent_harness/web:cli -- serve "$PWD/typescript/examples/openai_hello/agents.toml" --host 0.0.0.0 --port 8000
bazel run //typescript/examples/openai_hello:worker
```

The test runs with `bazel test //typescript/examples/openai_hello/...`.
