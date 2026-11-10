# Hello-world Strands Agents agent (TypeScript)

A conversational agent built with **Strands Agents** on the TypeScript harness. It chats in plain
text and uses every kind of tool a harness Strands agent takes. It shows the Strands integration
end to end: the model call, the Strands model activity where the harness translates the raw stream,
and the live turn stream the web UI draws. It is the TypeScript twin of
[`python/examples/strands_hello`](../../../python/examples/strands_hello), without the sandbox: the
Temporal Strands plugin for TypeScript has no `TemporalSandbox`.

The integration is the Temporal Strands plugin from
[`temporalio/ai-integrations`](https://github.com/temporalio/ai-integrations), vendored into the
harness with a streaming seam added: the workflow half at
`@temporalio/agent-harness/ai-sdks/strands-agents`, the worker half at
`@temporalio/agent-harness-worker/ai-sdks/strands-agents`, and the harness glue at
`.../ai-sdks/strands-agents-harness` in each package.

## What it demonstrates

- **`harnessStrandsAgent(runner, ...)`** builds a Strands `TemporalAgent` wired to the harness.
  Each model call runs in the plugin's model activity. There the harness observer turns the raw
  Strands stream into `model_interaction_started` → `thought_summary` / `reply_delta` /
  `tool_requested` → `model_interaction_ended` (with token usage) on the current turn.
- **One tool of each kind**, all adapted with `asStrandsTool(runner, ...)` so each call runs
  through `runner.runTool`: the approval policy, `tool_start` / `tool_end` / `tool_error`, and
  steering.

  | Tool | Kind | Where it runs |
  |---|---|---|
  | `get_weather` | `tool` with `execute` | inline, in the workflow |
  | `get_local_time` | `activityTool` | on the worker, as a durable activity (reading the clock is nondeterministic); its body is in `worker.ts` |
  | `ask_user` | `tool` without `execute` (a callback tool) | in the console: the call waits in the workflow until you answer |

- **Human input without Strands interrupts.** `ask_user` is a harness callback tool: it publishes
  `callback_requested`, waits durably in the workflow, and returns your answer as the tool's
  result. A harness Strands agent refuses Strands interrupts; callback tools and the approval
  policy take their place.
- **Mid-turn steering.** `ask` accepts messages mid-turn (`MidTurn.ACCEPT`). A message sent while
  the agent is working joins the open turn, and the handler queues it with `runner.steer(...)`
  instead of starting another run. The harness appends it to the next tool result the model sees,
  so the agent changes course without stopping.
- **The reply text.** A Strands `AgentResult`'s `toString()` also prints the model's reasoning, so
  `ask` returns the text blocks of `result.lastMessage`.

## Layout

| File | Role |
|---|---|
| `agents.ts` | `StrandsHelloAgentTs`: the harness agent and its tools. |
| `worker.ts` | Worker hosting the workflow: `StrandsPlugin` (OpenAI model, harness observer) and `AgentHarnessPlugin` (with `get_local_time`'s activity). |
| `agents.test.ts` | The agent end to end on a dev server with a scripted model: a message sent while `ask_user` waits steers the turn. |
| `agents.toml`, `StrandsHelloAgentTs.schema.json` | Registry entry that makes this agent selectable in the shared web UI, and the agent's schema; generated from `BUILD.bazel` (`bazel run :registry_update`). |

## Run it

Prereq: from the repo root, `cp .env.example .env.local` and set `OPENAI_API_KEY` (and your Temporal
connection profile). Then, each in its own terminal:

```sh
just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. builds the Svelte UI, then serves API + UI on http://localhost:8000
just worker            # 4. the agent worker
```

Or `just dev` runs the session manager, server and worker in one terminal against a running
Temporal.

Open http://localhost:8000, pick **Strands Hello (TypeScript)**, and try:

- *"What's the weather in Paris, and what time is it there?"* — an inline and an activity tool.
- *"Plan a weekend trip for me."* — it asks you something with `ask_user`; answer in the console.
- Steer it: ask *"What's the weather and local time in Paris, Tokyo and Lima?"*, and while it is
  calling tools send *"Skip Tokyo, and use Celsius."* The next tool result carries your message,
  and the agent adjusts the rest of its answer.
