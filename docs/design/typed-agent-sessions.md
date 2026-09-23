# Typed agent sessions: a Svelte client over the event stream, typed from the agent class

> Status: **design; not built.** Builds on [`per-message-events.md`](per-message-events.md) (the
> `message_id` envelope the client keys on) and [`observable-agent-state.md`](observable-agent-state.md)
> (the snapshot + patch stream the client folds). Related to
> [`rendering-agent-state.md`](rendering-agent-state.md), which puts a runtime `value_schema` on
> `AgentStateSnapshot` — see **Open decisions**.

## The problem

Every consumer of the event stream rebuilds the same client from scratch. The packaged console
does it in `ui/src/lib/state/agentRun.svelte.ts` (2,100 lines, roughly half of it connection
handling and frame buffering), the chat-server does it in `third-party-platforms/chat-server/src/harness.ts`,
and `examples/tictactoe/play.html` does it again by hand. Each one re-learns the same lessons:
one attach per session, resume from `resume_offset`, back off and wake early on a send, poll
`agent_status` while idle instead of re-attaching (Temporal caps in-flight updates at 10 per
workflow, and a parked poll can't be reclaimed), de-duplicate frames, key replies by
`message_id` rather than `turn_id`.

None of them is typed against the agent it talks to. `ui/src/lib/api/types.ts` is a hand-written
copy of `events.py` that drifts, and a UI that renders an agent's observable state — the thing
people actually want to build a UI around — reads it as untyped JSON.

## The goal

```svelte
<script lang="ts">
  import { AgentSession } from "$lib/harness/svelte";
  import type { TicTacToe } from "./TicTacToe";      // generated

  let { sessionId } = $props();
  const agent = new AgentSession<TicTacToe>({ get sessionId() { return sessionId; } });
  const board = $derived(agent.state.board);         // Board | undefined until the snapshot arrives
</script>

{#if board}
  {#each board.cells as cell, i}
    <button disabled={cell !== null || board.status !== "playing"}
            onclick={() => agent.sendMessage("play", { cell: i + 1 })}>{cell ?? ""}</button>
  {/each}
{/if}
```

A UI developer gets a reactive message list, agent and per-message status, pending approvals and
callbacks, the agent's observable state typed from its Python models, and a `sendMessage` checked
against each handler's input model. Connection handling is written once, in the core.

## Inspiration: `@ai-sdk/svelte`

The AI SDK is inspiration only — there is no integration and no `ChatTransport` adapter. Its model
(the client owns `messages[]` and re-POSTs history per request) is the inverse of ours (the
workflow owns the conversation; the stream is a durable, offset-addressed log), so an adapter
would be lossy exactly where the harness is richer: queued/joined dispositions, auto-approval
evaluations, observable state, subagents.

What is worth taking is the shape of `@ai-sdk/svelte@5`:

- **The binding is almost empty.** `chat.svelte.ts` is ~50 lines. All behaviour lives in a
  framework-independent `AbstractChat` in the `ai` package; a framework plugs in only by
  implementing a `ChatState` interface (reactive fields plus `pushMessage` / `replaceMessage` /
  `snapshot`). React and Vue implement the same interface.
- **A class, not a hook.** `new Chat({...})`, read through getters backed by `$state`.
- **One reactive write per chunk.** Chunks mutate a private working message; `write()` then calls
  `replaceMessage(index, message)` once.
- **A serial job queue** orders stream chunks against user actions (`addToolOutput`, approvals).
- **`KeyedStore`** — a `SvelteMap` keyed by id, set up through context, so components that
  construct the same id share one state.

Where we deliberately differ:

- **Updates land on any message, not only the last.** A refcounted turn can stream two messages at
  once, a queued message sits earlier in the list, and an approval can resolve on an older message.
  `AbstractChat` assumes the active response is the last message; our core keeps a
  `message_id → index` map and replaces by index.
- **The connection is long-lived.** An AI SDK chat has nothing open between requests; we hold an
  `attach`. Sharing one attach per session is a correctness concern (the update cap), not polish.
- **The raw frame log is exposed alongside messages**, because the console is an inspector
  (replay, graph, cost, audit panels), not only a chat.
- **Left out:** a single `status` enum (we need agent-level `idle | busy` plus per-message
  `queued | running | done | error`), a writable `messages` setter, `regenerate`, `stop` as
  "abort the fetch" (detaching doesn't stop an agent), and `sendAutomaticallyWhen` (the workflow
  continues its own loop after an approval or callback).

## Settled

### Core and binding

`AgentSessionCore<A extends AgentSchema = UntypedAgent>` owns:

- the transport: `attach`, `submitMessage`, `approve`, `callbackResult`, `close`, `status`;
- the connection lifecycle moved out of `AgentRunController`: one attach per session, backoff,
  waking early on a send, polling `agent_status` while idle, frame de-duplication, `resume_offset`,
  catch-up chunking, optimistic user messages reconciled to `message_id`;
- a serial queue for every mutation;
- an **incremental** reducer, `applyFrame(working, frame) → touched message ids`, replacing
  today's full `buildTranscript(frames)` rebuild on every commit (the comment on `total` in
  `agentRun.svelte.ts` records the O(n²) hydration this already caused).

Reactivity comes in through a `SessionState` interface, as `ChatState` does for `AbstractChat`:

```ts
interface SessionState<A extends AgentSchema> {
  connection: "idle" | "connecting" | "live" | "backoff" | "error";
  agentStatus: "idle" | "busy";
  error: HarnessError | undefined;
  messages: HarnessMessage<A>[];
  frames: AgentSseFrame[];
  replaceMessage(index: number, m: HarnessMessage<A>): void;
  pushMessage(m: HarnessMessage<A>): void;
  appendFrames(batch: AgentSseFrame[]): void;
  setAgentState(stateId: string, doc: unknown): void;
  snapshot<T>(x: T): T;
}
```

After each batch (cut per animation frame), the core calls `replaceMessage` once per touched
message.

The Svelte binding:

- **`$state.raw`, not deep `$state`.** The core always replaces whole messages, so deep proxies buy
  nothing and make replaying thousands of frames expensive. `snapshot` becomes the identity.
- **One core per session.** A reference-counted keyed store, created by `createHarnessContext()`,
  so `TranscriptPanel`, `AgentStatePanel` and an approval button deep in the tree share one attach.
- **The connection follows observation**, via `createSubscriber` from `svelte/reactivity`
  (Svelte ≥ 5.7): the attach opens when an effect first reads the session and closes when nothing
  reads it. Unmounting the last reader releases its update slot with no teardown code.
- **Getter options** (`get sessionId()`), so switching sessions re-attaches.

### The message model

- `message_accepted` → a user message, `id = message_id`, metadata `{handler, payload, disposition}`.
- The assistant message shares that `message_id`. Parts: `step` per `model_interaction_started`
  (usage from `model_interaction_ended` into metadata), `reasoning` from `thought_summary`,
  `text` from `reply_delta`, `source` from `text_annotation`, and a typed `output` from
  `message_handler_end`, typed by the handler's output model.
- Tool parts keyed by `tool_id`, state machine
  `requested → [approval-requested → evaluating* → approved | denied] → running → [awaiting-client] → done | failed`.
  `evaluating` (an auto-approval bracket is open) is distinct from `approval-requested` (a person
  must act). Evaluations stay on the part for audit UIs.
- `state_snapshot` / `state_patch` → not messages; a `state[state_id]` map folded with the
  existing `ui/src/lib/state/jsonPatch.ts`.
- `subagent_*` → a `subagent` part on the parent message; child messages grouped by `agent_id`.

### Tool inputs and outputs stay opaque JSON

Tool parts are `{ toolName: string; input: unknown; output: unknown }`. Clients rarely need tool
I/O typed, and the one case that matters — callback tools — already requires the client author
to implement the contract, so they validate it where they implement it (zod or similar). This
keeps `run_tool` unchanged, needs no tool declarations anywhere, and leaves runtime-configured
tools (a user adding or removing an MCP server through a handler) entirely unconstrained by the
harness. See **Rejected** for the tool-typing design this replaces.

### State is declared on the class

```python
@workflow.defn(name="TicTacToeAgent")
@agent.defn
class TicTacToeAgentWorkflow:
    board = agent.state(game.Board)          # state_id "board", from the attribute name
    # agent.state(Board, initial=lambda: Board(...)) when the default constructor isn't right

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(config, stream=WorkflowStream(), ...)

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def new_game(self, message: NewGame) -> TextReply:
        with self.board.mutate() as draft:
            ...
```

- `agent.state(...)` returns a `StateDecl[T]` descriptor. `__set_name__` takes the state id from
  the attribute name, so the id can't drift from the attribute. An overloaded `__get__` returns the
  declaration when read from the class (what codegen reads) and the instance's `StateRef[T]` when
  read from an instance.
- Refs are stored **on the instance, never on the descriptor.** A class attribute is shared by every
  workflow instance on a worker; holding the ref on the descriptor would leak one workflow's board
  into another.
- `@agent.defn` records the declarations next to `_discover_handlers`, and rejects duplicate ids
  and non-`HarnessState` types at import time — the same place it already validates handlers and
  the `run` signature.
- The `AgentWorkflowRunner` constructor gets the instance (not only its class) from the stack walk
  it already does (`_enclosing_workflow_class`), registers every declared state and publishes its
  snapshot — at the same point `runner.state()` does today. Its signature doesn't change.
- `runner.state()` stops being public (becomes `_register_state`). Without that, the generated
  schema could not claim to list every state an agent publishes. `StateHost().state(...)` stays as
  the unit-test primitive; it publishes to no agent stream.

**Where it must be written, and what happens otherwise.** Calling `agent.state(...)` anywhere is
harmless, but it only does anything as a class attribute. The wrong placement does not half-work —
it fails at the line that uses it, as a type error:

```python
def __init__(self, config):
    self.board = agent.state(game.Board)   # wrong place
...
with self.board.mutate() as d:             # pyright: "StateDecl[Board]" has no attribute "mutate"
```

That is the same way `dataclasses.field()` and pydantic's `Field()` teach where they belong.

`HarnessState` is already strict, which is what makes it generate cleanly: `extra="forbid"` becomes
`additionalProperties: false`, and `StateSchemaError` rejects fields the harness can't own at
class definition. Sets are no longer allowed (see
[`../internal/observable-agent-state-status.md`](../internal/observable-agent-state-status.md) §5.1),
so no set-to-array handling is needed.

### Codegen

Two artifacts, one pipeline: Python writes JSON Schema, a TypeScript tool writes types.

1. **Per agent.** `agent_schema(cls)`, next to `agent_handlers`, returns handlers and declared
   states. `temporal-agent-harness schema module:Class` writes one document:

   ```
   { agent, handlers: { name: { input, output, mid_turn, description } }, states: { id: schema }, $defs }
   ```

   - One `models_json_schema` call, so all models share one `$defs` and `Mark` is emitted once.
   - Handler inputs use `mode="validation"` (a field with a default is optional going in); handler
     outputs and states use `mode="serialization"` (what `model_dump(mode="json")` puts on the wire,
     so every field is present). `code_mode/stubs.py` has shipped this exact bug once — defaulted
     fields rendered as required — so it gets its own test.

2. **Protocol types.** `TypeAdapter(AgentEvent).json_schema(mode="serialization")` through the same
   generator, replacing the hand-written event types in `ui/src/lib/api/types.ts`. The SSE frame
   type is derived in TypeScript, not generated, matching the flattening in `web/app.py`:

   ```ts
   type SseFrame = AgentStreamItem extends infer E ? E & Omit<AgentEvent, "event"> & { resume_offset: number } : never;
   ```

   `stream_error` frames stay hand-written: the server synthesizes them and no pydantic model exists.

The TypeScript side is a small CLI, `harness-codegen <schema.json> -o <Agent>.ts`:
`json-schema-to-typescript` over `$defs` (docstrings → JSDoc, `Literal` → string unions,
`ge`/`le` → JSDoc tags) plus a ~100-line template that writes the `{ handlers, states }` mapping
type — the part no off-the-shelf tool produces. Generated files are checked in; `just codegen`
regenerates them and a CI job fails on a diff, the same pattern as the committed `ui/dist`.

For TicTacToe the output is:

```ts
// GENERATED from examples.tictactoe.workflow:TicTacToeAgentWorkflow — do not edit.
export type Mark = "X" | "O";
export type Status = "playing" | "X_wins" | "O_wins" | "draw";
export interface Move { mark: Mark; cell: number }
/** The whole game: nine cells, whose turn it is, who the agent is, and the outcome. */
export interface Board {
  cells: (Mark | null)[];
  to_move: Mark;
  agent_mark: Mark;
  status: Status;
  moves: Move[];
}
export interface NewGame { agent_goes_first?: boolean }
export interface PlayMove { /** @minimum 1 @maximum 9 */ cell: number }
export interface TextReply { text: string }

export interface TicTacToe {
  handlers: {
    new_game: { input: NewGame;  output: TextReply };
    play:     { input: PlayMove; output: TextReply };
  };
  states: { board: Board };
}
```

The library's default type parameter is an untyped `AgentSchema`, so the console keeps rendering
any agent it discovers at runtime through `agent-interface`. Codegen is opt-in.

## Phases

Phases 1 and 2 are Python only and useful on their own. Phases 3 and 4 share one generator.
Phase 5 doesn't depend on 1–4: it can start in parallel against the untyped schema and pick up
generated types when they land.

### Phase 1 — declare state on the class

- **Build:** `agent.state` / `StateDecl`; declarations recorded by `@agent.defn`; the runner
  registers them from the instance found by the stack walk; `runner.state()` made private.
- **Migrate:** `examples/tictactoe/workflow.py`, `examples/monty/workflow.py`,
  `examples/monty/conversational_workflow.py`, `examples/auto_mode/workflow.py`,
  `tests/harness/test_observable_state.py`. Docs showing `runner.state(...)`: the
  `AgentWorkflowRunner.state` docstring, the `board.py` and `trip_board.py` module docs, the
  tictactoe and monty READMEs, `observable-agent-state.md`. Code Mode needs no change:
  `injections={"board": self.board}` still runs in `__init__`, after the runner exists.
- **Tests:** two workflows of one type on one worker never share state; class access gives the
  declaration and instance access the ref; a duplicate id or non-`HarnessState` type fails at
  import naming the attribute; declarations evaluate cleanly under the workflow sandbox; the
  snapshot is published at runner construction (`test_observable_state` passes apart from the
  declaration); tictactoe and monty pass end to end against the time-skipping server.
- **Done when** no public way remains to publish state that isn't declared on the class.

### Phase 2 — `agent_schema(cls)` and the schema CLI

- **Build:** `agent_schema`; the `schema` subcommand in `web/cli.py`.
- **Tests:** a golden schema for tictactoe; the validation/serialization split
  (`NewGame.agent_goes_first` absent from `required`, every `Board` field present); stable,
  readable `$defs` names.
- **Done when** the CLI produces a deterministic, diffable schema for every example agent.

### Phase 3 — TypeScript generator

- **Build:** `harness-codegen`; `just codegen`; the CI drift check.
- **Tests:** a golden `TicTacToe.ts`; `tsc --noEmit` on the output; vitest `expectTypeOf` type tests
  (as `@ai-sdk/svelte` does in `completion.svelte.test-d.ts`) for `sendMessage` and `state`.
- **Done when** changing a field on `Board` without regenerating fails CI.

### Phase 4 — protocol types from `events.py`

- **Build:** the event schema through the same generator; the derived `SseFrame`; `types.ts`
  re-exports the generated event types.
- **Tests:** `just app-check`; `ui/src/lib/mock/scenarios.ts` type-checks against generated frames.
- **Done when** a field added to an event in `events.py` reaches the UI through `just codegen` alone.

### Phase 5 — session core and Svelte binding

- **Build:** `AgentSessionCore`, `SessionState`, the incremental reducer and transport under
  `ui/src/lib/harness/core/`; `SvelteSessionState`, `AgentSession`, the keyed store and
  `createSubscriber` lifecycle under `ui/src/lib/harness/svelte/`.
- **Tests:** reducer tests over the mock scenarios — concurrent messages in one turn, a queued
  message, an approval resolving on an older message, a replay from offset 0; core tests against a
  fake transport — backoff, wake on send, the idle poll, de-duplication. UI tests here are SSR-only
  and can't catch client-only crashes, so logic stays in the core where plain unit tests reach it,
  and the binding is exercised once in the real app.
- **Done when** the core runs a live session end to end against `just server`, and a 1,500-frame
  replay hydrates without O(n²) rebuilds.

### Phase 6 — dogfood

- **Build:** `AgentChatPanel` and `TranscriptPanel` on an untyped `AgentSession`, with
  `AgentRunController` keeping replay, graph and cost on `session.frames`; a typed TicTacToe view on
  `AgentSession<TicTacToe>`.
- **Done when** the console behaves as before and the TicTacToe view has no hand-written event parsing.

## Open decisions

- **Package home.** Suggested: prototype in `ui/src/lib/harness/`, extract to a workspace package
  once a second consumer (chat-server, the TicTacToe view) needs it. The generator moves with it.
- **State read before the runner exists.** Suggested: create the ref lazily on first access and
  treat a mutation before the runner exists as changing the initial value, so the first snapshot
  includes it. The alternative is an ordering rule, which is the kind of placement rule this design
  avoids.
- **Where the typed TicTacToe view lives.** Suggested: a small Svelte page in `examples/tictactoe/`.
  `play.html` stays — being a single double-clickable file is its purpose.
- **Relation to `rendering-agent-state.md`.** That design puts a runtime `value_schema` (with
  `x-harness-display` hints) on `AgentStateSnapshot`. Both should come from the same
  `agent_schema` call; unknown `x-` keys pass through `json-schema-to-typescript`, so they don't
  conflict.

## Risks

- **Getting the instance from the stack walk.** `_enclosing_workflow_class` already finds `self` in
  the `@workflow.init` frame; returning the instance is small, but a runner built outside
  `__init__` would then behave differently. Needs a test and an error that names the fix.
- **`$defs` naming.** `models_json_schema` qualifies clashing names by module
  (`examples__tictactoe__board__Board`). We need a rule that keeps plain names and fails loudly on a
  real collision.
- **Stale types across deploys.** A session started on an older build can replay snapshots with an
  older shape. Accepted for now; a schema hash on `AgentStateSnapshot` would detect it later.

## Rejected, with reasoning

- **An AI SDK integration or `ChatTransport` adapter.** Lossy where the harness is richer (see
  **Inspiration**), and the approval flow doesn't fit: useChat expects to re-POST after an
  approval, while the workflow continues on its own.
- **Registering state (and tools) through `@agent.defn(states=..., tools=...)`.** Splits an agent's
  configuration between the decorator and the runner constructor in `__init__`, which is surprising
  in the wrong way.
- **Registering them through the `AgentWorkflowRunner` constructor.** The constructor only runs
  inside a live workflow, so static tooling can't see its arguments. Codegen would have to boot a
  test server and worker and query a started workflow — accurate, but it puts Temporal
  infrastructure inside codegen, and the result would depend on the `AgentConfig` it was started
  with. Handler discovery already chose static reflection over the class, and `subagent_toolset`
  relies on it; state follows the same rule.
- **Typed tools.** Explored in full before being dropped: class-level `agent.toolset(...)`
  declarations, a `ToolNotDeclared` check in `run_tool` (and, before that, `BoundTool` capability
  types only obtainable from a class attribute), `code_mode_tool` injections moved to dispatch time
  so it could be built at class level, and `agent.dynamic_toolset(dispatch=...)` for tools added at
  runtime (data records routed through one declared dispatcher, with the active set published as
  observable state). It worked, but the client value was small next to the complexity, and it
  constrained runtime tool configuration. If typed tools come back, that is the design to start
  from.
