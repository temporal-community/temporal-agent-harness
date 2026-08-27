# Core concepts: turns, tools, the two loops, and SDK integrations

The mental model for how a harness agent executes, and what building an SDK integration involves.

## The turn — the unit of execution

A **turn** is one inbound message processed to completion — the harness's atomic unit.

- Begins when the runner pops a queued message and calls the matching `@agent.accepts` handler;
  ends when that handler returns (→ `reply`) or raises (→ `error`).
- **Strictly sequential** — one handler is awaited to completion before the next. This is what makes
  "the current turn" unambiguous, so activity-side event publishing always knows which turn it
  belongs to.
- Bracketed by guaranteed events: `turn_started` → (`reply` | `error`) → **always** `turn_end`
  (emitted in a `finally`) — the single reliable end-of-turn signal.
- A raising handler does **not** end the session — the error becomes an `AgentError` event and the
  loop continues. The workflow is long-lived, spanning many turns until the `close` signal.
- Identity: a `turn_id` (uuid) + a monotonic `turn_number`, both stamped on every event.
- Nested spans inside a turn: `model_interaction_started/ended` pairs (one per model call, with
  `TokenUsage`) and `tool_*` brackets (one per tool call).

## Driving an agent — the message envelope and interface discovery

If a turn is the unit of execution, the **`AgentMessage` envelope** is how a caller *starts* one.
Every inbound message — from the packaged UI, a bespoke UI, or a parent agent — is the same shape,
delivered on the `send_agent_message` update:

```
AgentMessage(
  type          = <@agent.accepts handler name>,   # selects the handler to run
  payload       = <that handler's input model, as JSON>,
  expected_turn = <n>,                              # optimistic concurrency
)
```

- **`type` = handler name is the universal routing contract.** The runner's validator
  (`_validate_send_agent_message`) enforces it *before* any state changes: an unknown `type` →
  `UnknownFunction`, a `payload` that fails the handler's pydantic input model → `MalformedMessage`,
  a stale `expected_turn` → `StaleTurn`. So the dispatch loop only ever sees a known handler + an
  already-coerced input. The handler's **return value becomes the turn's `reply` event** (see below).
- **Discovery, not hardcoding.** A client learns an agent's callable surface at runtime from the
  `agent_interface` query — each handler's name + input/output JSON schemas (tool-style) — and
  `operator_interface` for slash commands. The packaged server exposes these at
  `GET /api/agent-interface/{session_id}` and `/api/operator-interface/{session_id}`. A generic UI
  can read the schema and format correct envelopes (even auto-generate a form) without knowing the
  agent in advance. This is the same interface a **parent agent** reads to call a subagent, and the
  same handlers `subagent_toolset` reflects over statically.
- **The packaged UI's conventions** (a pragmatic layer on top of the contract, in `web/app.py`):
  a plain-text chat message maps to `ask` with `{"text": …}` — so the chat box assumes a handler
  `ask(TextMessage) -> TextReply` (every conversational example exposes exactly that). Slash
  commands map to the reserved `slash` channel. Any other handler is reachable via a structured
  `{"type": …, "payload": {…}}` message. **To work with the packaged chat UI out of the box, expose
  `ask(TextMessage) -> TextReply`;** an agent with a different handler (e.g. Monty's
  `run_script(RunScript)`) needs the structured path or a custom UI.

## The AgentEvent stream — a turn's observable output

Everything a turn does surfaces as **`AgentEvent`s** on a single durable stream — the harness's one
observability surface, identical across every SDK. This is the concept behind all the event names
above (`turn_started`, `reply_delta`, `tool_*`, …).

- **An `AgentEvent` is a typed record of one thing that happened** — a turn boundary, a model
  interaction, a tool call, an approval, a reply delta. It's a semantic *payload* (e.g.
  `ReplyDelta(text=…)`, `ToolStart(…)`) wrapped in an *envelope* that stamps routing metadata the
  harness controls: `agent_id` / `turn_id` / `turn_number` / `timestamp`. Producers build only the
  payload and *cannot* set the envelope, so routing metadata is trustworthy by construction.
- **The vocabulary is closed and discriminated.** `AgentStreamItem` unions the ~two dozen event
  types (`turn_*`, `model_interaction_*` with `TokenUsage`, the full `tool_*` lifecycle incl.
  approvals, `subagent_*`, `reply_delta`/`thought_summary`/`text_annotation`, terminal
  `reply`/`error`, `operator_command_*`) keyed on `type`. The *same* vocabulary for every agent
  regardless of which SDK wrote the loop → one UI, one analytics pipeline across the fleet. (Defined
  in `agent_protocol/events.py`.)
- **One topic, two producers.** All events publish to the single `turn_events` topic on the agent's
  `WorkflowStream`: **in-workflow** via `_pub` (lifecycle, the approval cascade, inline-tool
  brackets, and — for a non-streamed model call — its `model_interaction_*` bracket) and **from
  inside activities** via `publisher_from_activity` (streamed `reply_delta`, `model_interaction_*`,
  activity-tool brackets). Raw provider tokens are folded into `AgentEvent`s
  *inside* the activity — the lowest-level thing that crosses the activity→workflow→client boundary
  is already a semantic event, never raw bytes.
- **It's a durable, replayable stream, not a fire-and-forget feed.** Each event is a Temporal Signal
  in workflow history, so the log is offset-addressed and reconstructed deterministically on replay;
  a consumer subscribes by `workflow_id`, reads from an offset, and resumes after a disconnect
  without loss (this backs the UI's play/pause replay). Each agent — root *and* every subagent — has
  its own stream; the UI-facing "stream" is a client-side **merge** of the whole agent tree.

Full mechanics and durability guarantees:
[`agentevent-workflow-stream.md`](agentevent-workflow-stream.md) (the primitive + durability) and
[`event-stream-and-storage.md`](event-stream-and-storage.md) (wire mechanics, merge, storage).

## The two loops (don't conflate them)

- **Outer turn loop — the harness's.** `await self._runner.run(self)` waits for messages, runs the
  turn lifecycle, publishes the reply, and loops. It owns message intake, queuing, turn events,
  and the `agent_status`/`agent_interface` queries.
- **Inner agentic loop — the author's (or the SDK's).** The model↔tools loop lives *inside* your
  `@agent.accepts` handler. You write it by hand (Gemini: a tool-calling `while` loop) or delegate
  it to an SDK (`Runner.run_streamed(...)` for the OpenAI Agents SDK). The harness deliberately does
  **not** own this loop — that's the part that differs per SDK.

```
runner.run(self)                    ← HARNESS turn loop (outer)
  └─ await self.ask(message)        ← your @agent.accepts handler
        └─ <your agentic loop>      ← model ↔ tools (yours, or the SDK's Runner)
  publish reply / turn_end
```

## Tools — one funnel, three flavors

Every tool call goes through `runner.run_tool(call_id, tool, …)` — the funnel that parks the
per-call ambient context (tool id, runner, injections). The **approval gate + `tool_start`/`tool_end`
events live in the tool's own dispatcher**, so all three flavors get gating + lifecycle events. The
flavors are the "**where does the tool run**" axis:

| Decorator | Runs | Notes |
|---|---|---|
| `@agent.tool_defn` | **inline**, in the workflow | deterministic, side-effect-free-ish work |
| `@agent.activity_tool_defn` | on a **worker**, as a durable activity | for I/O / nondeterminism / long-running. Produces two objects: an in-workflow *dispatcher* (gate + `execute_activity`) and a generated `@activity.defn` *body* (real work + event publishing); register the body via `agent.tool_activity(t)`. |
| `@agent.callback_tool_defn` | on an **external client** | body is a declaration only; the call pauses in-workflow and emits `callback_requested`; a client posts the result back and the turn resumes. (See **Callback tools** below.) |

Approval policy is resolved as: caller's `AgentConfig.approval_policy` if given, else the agent's
required `approval_policy_default`, then mutable at runtime via `runner.set_approval_policy(...)`.
`inherently_safe` on a tool is only a *hint* — the policy decides. (See
`human-in-the-loop-tool-approvals.md`. Note: the packaged UI can *resolve* approvals and read the
live policy, but has no control to *set* the policy at session creation.)

## Callback tools — pausing a turn for an off-worker result

`@agent.callback_tool_defn` (the "runs on an external client" flavor above) deserves its own mental
model: **an inline tool that parks the whole turn in-workflow until a client supplies the result.**
It's how an agent running in the cloud calls a tool that must run wherever the needed resource lives
— a file on the user's laptop, a photo on their phone, or the human themselves (`ask_user`).

- **The tool has no body.** The author declares only a signature and a *concrete return type* (the
  body is literally `...`). That return type is the contract: its JSON schema is handed to the
  client, and the client's result is validated/coerced against it before it becomes the return value.
- **Calling it parks the workflow, not an activity.** Because it's inline (not `activity_tool_defn`),
  the wait is a `workflow.wait_condition` — it consumes no activity / `start_to_close_timeout` and can
  sit durably for hours or days across worker restarts. That "no activity" is exactly what makes an
  open-ended human-in-the-loop wait cheap.
- **A typed pending-entry, pushed *and* queryable.** On call it registers a `_CallbackEntry` (keyed
  by per-call `tool_id`) in workflow state, publishes a `callback_requested` event (args + expected
  `output_schema`), then waits. Many can be pending at once — a turn's tool calls run concurrently,
  each on its own `tool_id`. A client discovers them two ways: the pushed event, or the
  `pending_callbacks` list on the `agent_status` query (the late-attach / reconnect path).
- **A client resolves it out-of-band.** Fulfillment is a Temporal *update* (`provide_callback_result`,
  keyed by `tool_id`), validated at the boundary — unknown id / already-resolved / output-type
  mismatch are all rejected, and a malformed result does *not* consume the one-shot gate (resubmit
  works). The `wait_condition` observes the state flip and the turn resumes: `ok` → the validated
  value is the tool's return; `error` / timeout / agent-close → a tool error the model sees (the turn
  never crashes).
- **It's off the agent-to-agent front door.** Like tool approval, `provide_callback_result` is
  excluded from `agent_interface` — a parent agent driving this one can't fabricate its child's
  callback result; fulfillment is a control-plane action by whoever attached.

Fulfillment is decoupled from any UI: the packaged server mirrors the update at
`POST /api/callback-result`, but any client speaking the update works. Examples:
`examples/react_agent` (`ask_user`) and the OpenCode coding agent. Machinery in
`harness/agent_workflow.py` (`callback_tool_defn`, `await_callback_result`, the `_CallbackEntry`
registry).

## Slash commands — the operator channel every agent gets (and extends cheaply)

A **slash command** is an operator/control action on the harness-reserved `slash` message type —
a channel *separate* from the agent's `@agent.accepts` message handlers. Every agent gets a packaged
set for free, and adding your own is a few lines.

- **Free defaults** (`slash_commands.default_commands()`): `/approvals` (set the tool-approval
  policy), `/allow-tools` (auto-approve named tools), `/status`, `/stop`.
- **Add your own** — pass a `slash_commands=[...]` list to the runner; keep the defaults by
  splatting them in:

```python
self._runner = AgentWorkflowRunner(
    config,
    stream=WorkflowStream(),
    slash_commands=[
        *slash_commands.default_commands(),      # keep the packaged ones
        model_slash_command(self._set_model),    # + your own
    ],
)
```

- Each entry is a `slash_commands.command(name=, label="/model", description=, handler=,
  argument=?, aliases=?)`. The **handler is synchronous** — `(SlashCommandContext, SlashCommand) ->
  TextReply`. Its `SlashCommandContext` exposes session state + mutators (`current_status`,
  `current_approval_policy`, `set_approval_policy(...)`, `close()`), so a command can *change* the
  session — e.g. a `/model` command calls back into the agent's own `set_model`, `/approvals` flips
  the policy, `/stop` calls `close`. `slash_commands.model_selector(...)` is a ready-made helper for
  the common "pick a model" case.
- **Typed arguments**: `argument=enum_arg(choices, …)` / `tool_names_arg()` give the UI a typed,
  validated input (choices/placeholder) checked before the handler runs.
- **Discoverable + audited**: the packaged set plus your additions are advertised on the
  `operator_interface` query (the UI renders them; contrast `agent_interface`, which advertises the
  `@agent.accepts` handlers), and each invocation is audited as `operator_command_*` events stamped
  `turn_number=0` — control-plane records, deliberately *not* agent turns.

Grounded in `harness/slash_commands.py`; see `examples/monty/conversational_workflow.py` for the
`/model` extension, and `what-the-harness-adds.md` (Control plane) for where this fits the value
story.

## What an SDK integration must provide

Adapting an AI SDK/framework onto the harness has **three responsibilities** (the event mapping is
the visible one, but not the only one):

1. **Wrap the SDK's model call as a durable Temporal activity** — so retries and credentials never
   leak into the workflow. (The workflow can't do network I/O.)
2. **Map the SDK's streamed output onto the harness event vocabulary** — an observer that translates
   the provider's parts into `model_interaction_*` / `reply_delta` / `thought_summary` /
   `text_annotation` / `tool_requested`.
3. **Bridge the SDK's tool calls back through `run_tool`** — so approvals and tool-lifecycle events
   still apply. (For a framework with its own tool loop, this is the hardest part — interpose
   `run_tool` into the framework's loop.)

**Streaming is not the seam — the model call is.** Only `reply_delta` / `thought_summary` /
`text_annotation` are streaming artifacts. `model_interaction_started` / `…_ended` (with
`TokenUsage`) and `tool_requested` are *facts about the turn* — the model was invoked, here is its
span, what it cost, what it asked for — equally true of a non-streamed call. So an integration must
emit them at the **model-invocation boundary for every model call**, not from inside its streaming
observer, or the event stream silently degrades (losing cost visibility) for anyone who doesn't
stream. Concretely: the OpenAI integration publishes them from the activity-side observer when
streaming and from a workflow-side `ModelCallObserver` (`model_call_observer_provider`) when not;
Pydantic AI streams internally whenever an `event_stream_handler` is set, so `run()` and
`run_stream()` are already identical; Gemini's Interactions path is streaming-only.

Two integrations, two provenances:
- **Gemini** (`ai_sdks/google_genai_plugin/`) — **harness-authored**; the harness wrote the
  activity-wrapping itself (not an official Temporal SDK integration).
- **OpenAI Agents SDK** (`ai_sdks/openai_agents/` + `ai_sdks/openai_agents_harness.py`) — a
  **vendored copy** of `temporalio.contrib.openai_agents` + generic seams
  (`stream_to_provider` / `observer_factory` for the streamed path,
  `model_call_observer_provider` for the non-streamed one), with harness specifics in the sibling
  module. (See `python-idioms-for-java-spring-devs.md` for decorator mechanics and the re-vendoring
  note.)

## Model output is not one blob — it's typed parts

A modern model call streams *differently-typed parts*, which is exactly why the event vocabulary has
distinct types. For Gemini/OpenAI the parts map roughly:

| Model part | Harness event |
|---|---|
| answer text (streamed) | `reply_delta` |
| reasoning/thinking **summary** | `thought_summary` |
| citations / grounding annotations | `text_annotation` |
| tool/function call (+ streamed args) | `tool_requested` → `tool_start`/`tool_end` |
| completion + token accounting | `model_interaction_ended` (+ `TokenUsage`) |

Each SDK integration is the **normalization layer** that folds its provider's part types onto these
neutral events — so any UI/consumer works unchanged across providers. "Thinking" vs. "the answer"
are genuinely different output channels (thinking is gated by config, e.g. Gemini
`thinking_summaries`); the harness keeps them as separate event types so consumers can render them
differently.
