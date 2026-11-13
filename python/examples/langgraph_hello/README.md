# Hello-world LangGraph agent

A conversational agent built with **LangGraph** on the harness: a weather chat with one tool of
each harness kind. It shows the LangGraph integration end to end: a reasoning model call, the
activity the plugin runs the model node in, where the harness translates the stream, and the live
turn stream the web UI draws, including thought summaries, an approval card, the agent-state
pane and a callback answer box.

The integration is the Temporal LangGraph plugin from
[`temporalio/ai-integrations`](https://github.com/temporalio/ai-integrations), vendored into the
harness as `temporal_agent_harness.ai_sdks.langgraph` with a tool-call seam and a streaming seam
added, and the harness's own module beside it.

## What it demonstrates

- **`graph(name, runner, ...)`** binds the registered two-node graph to the agent, once,
  in `@agent.init`; `ask` then invokes it per message on the conversation's thread, as any
  LangGraph code would. The `agent` node calls `gpt-5.1` with reasoning summaries in an activity,
  where the harness observer turns the stream into `model_interaction_started` →
  `thought_summary` / `reply_delta` / `tool_requested` → `model_interaction_ended` (with token
  usage) on the current turn.
- **One tool of each kind**, all in one prebuilt `ToolNode` that runs in the workflow. The
  plugin gives it `harness_tool_call`, so each call runs through `runner.run_tool`: the approval
  policy, `tool_start` / `tool_end` / `tool_error`, and steering.

  | Tool | Kind | Where it runs |
  |---|---|---|
  | `get_weather` | `@agent.activity_tool_defn` | on the worker, as a durable activity |
  | `remember` | `@agent.tool_defn` | inline, in the workflow; writes the agent's `Notes` state through an `Injected[StateRef[Notes]]` the model can't see |
  | `ask_user` | `@agent.callback_tool_defn` | in the console: the call waits in the workflow until you answer |

- **Human input without LangGraph interrupts.** `ask_user` is a harness callback tool: it publishes
  `callback_requested`, waits durably in the workflow, and returns your answer as the tool's
  result. A harness graph refuses `interrupt()`; callback tools and the approval policy take its
  place.
- **Mid-turn steering.** `ask` accepts messages mid-turn (`MidTurn.ACCEPT`). A message sent while
  the agent is working joins the open turn, and the handler queues it with `runner.steer(...)`
  instead of starting another run. The harness appends it to the next tool result the model sees.
- **Approvals.** The agent's policy is `ToolApprovalPolicy.allow_inherently_safe()`. `get_weather`
  and `ask_user` are inherently safe and run freely; `remember` waits for you to approve it, and
  each save shows up as a state patch in the agent-state pane.

## Layout

| File | Role |
|---|---|
| `workflow.py` | `LangGraphHelloAgent`: the harness agent, its tools, the `Notes` state, and the graph. |
| `worker.py` | Worker hosting the workflow: `LangGraphPlugin` (the graph, `harness_tool_call`, the harness observer) and `AgentHarnessPlugin` (with `get_weather`'s activity). |
| `agents.toml`, `LangGraphHelloAgent.schema.json` | Registry entry that makes this agent selectable in the shared web UI, and the agent's schema; generated from `BUILD.bazel` (`bazel run :agents_update`). |

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

Open http://localhost:8000, pick **LangGraph Hello**, and try:

- *"What's the weather in Paris?"* — the reply streams in and the `get_weather` activity shows live.
- *"Remember that I prefer Celsius."* — an approval card appears; approve it and the note lands in
  the agent-state pane.
- *"What's the weather where I live?"* — the agent asks you which city with `ask_user`; answer in
  the console.
- Steer it: ask *"What's the weather in Paris, Tokyo and Lima?"*, and while it is calling tools
  send *"Skip Tokyo."* The next tool result carries your message, and the agent adjusts its answer.
