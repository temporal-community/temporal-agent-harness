# Hello-world Strands Agents agent

A conversational agent built with **Strands Agents** on the harness. It chats in plain text and
uses every kind of tool a harness Strands agent takes. It shows the Strands integration end to
end: the model call, the Strands model activity where the harness translates the raw stream, and
the live turn stream the web UI draws.

The integration is the Temporal Strands plugin from
[`temporalio/ai-integrations`](https://github.com/temporalio/ai-integrations), vendored into the
harness as `temporal_agent_harness.ai_sdks.strands_agents` with a streaming seam added, plus the
glue in `temporal_agent_harness.ai_sdks.strands_agents_harness`.

## What it demonstrates

- **`harness_strands_agent(runner, ...)`** builds a Strands `TemporalAgent` wired to the harness.
  Each model call runs in the plugin's model activity. There the harness observer turns the raw
  Strands stream into `model_interaction_started` → `thought_summary` / `reply_delta` /
  `tool_requested` → `model_interaction_ended` (with token usage) on the current turn.
- **One tool of each kind**, all adapted with `as_strands_tool(runner, ...)` so each call runs
  through `runner.run_tool`: the approval policy, `tool_start` / `tool_end` / `tool_error`, and
  steering.

  | Tool | Kind | Where it runs |
  |---|---|---|
  | `get_weather` | `@agent.tool_defn` | inline, in the workflow |
  | `get_local_time` | `@agent.activity_tool_defn` | on the worker, as a durable activity (reading the clock is nondeterministic) |
  | `ask_user` | `@agent.callback_tool_defn` | in the console: the call waits in the workflow until you answer |
  | `sandbox_shell`, `sandbox_file_editor` | vended by `TemporalSandbox("scratch")` | on the worker's `scratch` sandbox, one activity per operation |

- **Human input without Strands interrupts.** `ask_user` is a harness callback tool: it publishes
  `callback_requested`, waits durably in the workflow, and returns your answer as the tool's
  result. A harness Strands agent refuses Strands interrupts; callback tools and the approval
  policy take their place.
- **Mid-turn steering.** `ask` accepts messages mid-turn (`MidTurn.ACCEPT`). A message sent
  while the agent is working joins the open turn, and the handler queues it with
  `runner.steer(...)` instead of starting another run. The harness appends it to the next tool
  result the model sees, so the agent changes course without stopping. `tool_end` keeps the
  tool's own output, and the turn stream shows `steering_enqueued` then `steering_claimed`.
- **Approvals.** The agent's policy is `ToolApprovalPolicy.allow_inherently_safe()`. The weather,
  time and ask-the-user tools are marked inherently safe and run freely. The sandbox tools are
  not, so each of their calls waits for you to approve it in the console.

## The scratch sandbox runs on the worker, unisolated

`worker.py` registers the `scratch` sandbox as Strands' `NotASandboxLocalEnvironment`: shell
commands and file edits run on the worker's machine, in its working directory, with the worker's
privileges. That keeps the example free of Docker, and every call needs your approval. For real
use, register a `DockerSandbox` or `SshSandbox` instead (`StrandsPlugin(sandboxes={...})`); the
workflow code does not change.

## Layout

| File | Role |
|---|---|
| `workflow.py` | `StrandsHelloAgent`: the harness agent, its tools, and the `TemporalSandbox`. |
| `worker.py` | Worker hosting the workflow: `StrandsPlugin` (OpenAI model, `scratch` sandbox, harness observer) and `AgentHarnessPlugin` (with `get_local_time`'s activity). |
| `agents.toml`, `StrandsHelloAgent.schema.json` | Registry entry that makes this agent selectable in the shared web UI, and the agent's schema; generated from `BUILD.bazel` (`bazel run :agents_update`). |

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

Open http://localhost:8000, pick **Strands Hello**, and try:

- *"What's the weather in Paris, and what time is it there?"* — an inline and an activity tool.
- *"Plan a weekend trip for me."* — it asks you something with `ask_user`; answer in the console.
- *"Write a haiku about Temporal to haiku.txt in your workspace, then show me the file."* — the
  sandbox tools; approve each call.
- Steer it: ask *"What's the weather and local time in Paris, Tokyo and Lima?"*, and while it is
  calling tools send *"Skip Tokyo, and use Celsius."* The next tool result carries your message,
  and the agent adjusts the rest of its answer.
