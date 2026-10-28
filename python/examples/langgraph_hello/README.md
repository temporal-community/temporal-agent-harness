# Hello-world LangGraph agent

The smallest interesting agent you can build on the harness with **LangGraph**: a weather chat with
one tool of each harness kind. It shows the harness **streaming** path for the LangGraph integration
end to end — from a reasoning model call, through the activity the vendored `temporalio-langgraph`
plugin runs the model node in, to the live turn stream the web UI consumes — and lights up every
console pane: thought summaries, an approval card, the agent-state pane and a callback answer box.

LangGraph's Temporal plugin is vendored as `ai_sdks.langgraph` (what differs from upstream is listed in its README): it runs
each node marked `execute_in="activity"` as an activity and the rest in the workflow. The harness
supplies a glue module (`ai_sdks/langgraph_harness.py`) for the seams the plugin lacks.

## What it demonstrates

- **Streaming via `stream_to_harness`.** The `agent` node sets it in its metadata, beside
  `execute_in`, and the plugin wraps the node. Inside the model
  activity it attaches a LangChain callback handler to the node's model calls and translates the
  stream into harness vocabulary — `model_interaction_started` → `reply_delta` / `thought_summary` …
  `model_interaction_ended` (with token usage) — heartbeating while the node runs.
- **Reasoning summaries.** The model is `gpt-5.1` with `reasoning={"effort": "medium", "summary":
  "auto"}`; its summaries stream as `thought_summary` events the console shows while it thinks.
- **`HarnessContext` as the graph's `context_schema`.** Each turn passes
  `HarnessContext(runner=self._runner, injections={"notes": self.notes})` to `run_turn`; it
  snapshots the turn's stream context for the activity node and keeps the runner and the
  injections in the workflow for the tool node.
- **One tool of each harness kind**, all in one prebuilt `ToolNode(..., awrap_tool_call=
  harness_tool_call)` that runs **in-workflow**, so every call goes through
  `AgentWorkflowRunner.run_tool` — where the approval gate and `tool_start`/`tool_end` events live:
  - `get_weather` is an `@agent.activity_tool_defn`: its body runs as a Temporal activity the
    worker registers with `AgentHarnessPlugin(tools=[get_weather])`. Inherently safe.
  - `remember` is an inline `@agent.tool_defn` that writes the agent's declared `Notes` state
    through an `Injected[StateRef[Notes]]` the model cannot see. It is not inherently safe, so
    the default policy sends it to you first (approval card), and each save shows up as a state
    patch in the agent-state pane.
  - `ask_user` is an `@agent.callback_tool_defn` with no worker body: the turn parks until you
    answer in the console.

## Layout

| File | Role |
|---|---|
| `workflow.py` | `LangGraphHelloAgent` — the harness agent; one `ask` handler, the `Notes` state, three tools, a two-node graph driven by `run_turn`. |
| `worker.py` | Worker hosting the workflow; `LangGraphPlugin` then `AgentHarnessPlugin` on the client. |
| `agents.toml`, `LangGraphHelloAgent.schema.json` | Registry entry that makes this agent selectable in the shared web UI, and the agent's schema it points at; generated from `BUILD.bazel` (`bazel run :agents_update`). |

There is **no per-example client**: like the other examples, this agent is driven by the shared
example stack — the packaged `SessionManagerWorkflow` worker plus the FastAPI app and web UI
(`temporal-agent-harness serve`).

## Run it

Prereq: from the repo root, `cp .env.example .env.local` and set `OPENAI_API_KEY` (and your Temporal
connection profile). Then, each in its own terminal:

```sh
just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. builds the Svelte UI, then serves API + UI on http://localhost:8000
just worker            # 4. the agent worker
```

Open http://localhost:8000, pick **LangGraph Hello**, and chat. Try, in order:

- *"What's the weather in Paris?"* — the reply streams in and the `get_weather` activity shows live.
- *"Remember that I prefer Celsius."* — an approval card appears; approve it and the note lands in
  the agent-state pane.
- *"What's the weather where I live?"* — the agent asks you which city, and continues with your
  answer.

Without `just`, the equivalent commands (from the repo root):

```sh
uv run --group examples temporal-agent-harness session-manager
uv run --group examples temporal-agent-harness serve examples/langgraph_hello/agents.toml --host 0.0.0.0 --port 8000
uv run --group examples python -m examples.langgraph_hello.worker
```

## Behavior and limits

The essentials are in the `langgraph_harness` module docstring; these are the rest.

- Each activity attempt is its own model interaction. A failed attempt's deltas stay on the
  stream, and one lost to a crash or timeout leaves its `model_interaction_started` unmatched.
  Tools and approvals survive crashes: no tool runs twice.
- The plugin checks a node's `execute_in` at registration but its timeouts only on its first
  call: an activity node with neither `start_to_close_timeout` nor
  `schedule_to_close_timeout` fails the first turn that reaches it.
- `tool_requested` carries the model's arguments as given, before validation. `stream_to_harness`
  publishes it as the model call ends, before `model_interaction_ended`; a call the model gave no
  id is requested by the workflow-side `ToolNode` under a fresh one.
- A tool that fails or is denied becomes an error `ToolMessage` the model sees. A result that
  isn't a string, `ToolMessage` or `Command` is sent as JSON.
- A failed turn keeps its completed steps in the checkpoint; the next message starts a fresh run
  with the failed message still in the conversation.
- Activity nodes get `state["messages"]` as plain dicts (the plugin passes activity values
  untyped). LangChain coerces them on the way into the model, but a returned `AIMessage`'s
  `usage_metadata` lands in `additional_kwargs`: read usage from `model_interaction_ended`,
  and `convert_to_messages` before `isinstance` checks.
- The checkpoint lives in workflow memory, keyed by the workflow id, with no continue-as-new.
  Turns that join an open turn (`MidTurn.ACCEPT`) run concurrently on that one thread and
  overwrite each other's checkpoint; use `REJECT` or `ENQUEUE` unless a graph tolerates it.
  Streaming dominates history, roughly 3.7 events per token watched; a 300-token reply per turn
  reaches Temporal's event limit after about 45 turns. Each activity node also receives the whole
  state, every message, as its input, so payloads grow with the conversation.
- Adding, removing or reordering activity nodes on an executed path breaks replay of running
  sessions; use `workflow.patched()` or Worker Versioning.
- The Functional API works the same way: declare `@entrypoint(context_schema=HarnessContext)`,
  set `"stream_to_harness": True` in the model task's `activity_options` (`execute_in="activity"`), run
  tools from a workflow-side task that invokes `ToolNode(..., awrap_tool_call=harness_tool_call)`
  and pass `entrypoint(name).copy({"checkpointer": InMemorySaver()})` to `run_turn`. An
  entrypoint is built once at import, so each workflow takes its own checkpointer that way; an
  activity task's value comes back untyped, so `convert_to_messages` the reply; and the plugin
  wraps a task in place, so construct it once per process. `tests/ai_sdks/langgraph` has the agent.
- A prebuilt graph such as `create_react_agent`'s needs `adopt_prebuilt`: the plugin keys
  nodes by module-level qualname and rejects its closure nodes, passes `runtime` only to a
  signature that names it, and cannot run its sync router in the workflow. Build it with
  `context_schema=HarnessContext`; the helper gates its `ToolNode` and streams its model nodes.
