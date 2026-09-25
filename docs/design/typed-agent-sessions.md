# Typed agent sessions: a client over the event stream, typed from the agent class

> Status: **built through phase 5**, plus a typed tic-tac-toe app on top of it. Phase 6 (moving
> the console onto the client) is not started. Builds on [`per-message-events.md`](per-message-events.md)
> (the `message_id` envelope the client keys on) and [`observable-agent-state.md`](observable-agent-state.md)
> (the snapshot + patch stream the client folds). Related to
> [`rendering-agent-state.md`](rendering-agent-state.md), which puts a runtime `value_schema` on
> `AgentStateSnapshot` — see **Open decisions**.

## The problem

Every consumer of the event stream rebuilt the same client from scratch. The packaged console does
it in `ui/src/lib/state/agentRun.svelte.ts` (2,100 lines, roughly half of it connection handling
and frame buffering), the chat-server does it in `third-party-platforms/chat-server/src/harness.ts`,
and the tic-tac-toe example did it again by hand in a single-file `play.html`. Each one re-learned
the same lessons: one attach per session, resume from `resume_offset`, back off and wake early on
a send, don't re-attach an idle session on a timer (Temporal caps in-flight updates at 10 per
workflow, and a parked poll can't be reclaimed), de-duplicate frames, key replies by `message_id`
rather than `turn_id`.

None of them was typed against the agent it talks to. `ui/src/lib/api/types.ts` was a hand-written
copy of `events.py` that had drifted, and a UI rendering an agent's observable state — the thing
people actually want to build a UI around — read it as untyped JSON.

## The result

```svelte
<script lang="ts">
  import { AgentSession } from "@temporal-agent-harness/svelte";
  import type { TicTacToeAgent } from "../../client_sdk/TicTacToeAgent";   // generated

  let { sessionId } = $props();
  const agent = new AgentSession<TicTacToeAgent>({ get sessionId() { return sessionId; } });
  const board = $derived(agent.states.board);   // Board | undefined until the snapshot arrives
</script>

{#if board}
  {#each board.cells as cell, i (i)}
    <button disabled={cell !== null || board.status !== "playing"}
            onclick={() => agent.sendMessage("play", { cell: i + 1 })}>{cell ?? ""}</button>
  {/each}
{/if}
```

A UI developer gets a reactive message list for every agent in the session's tree, agent and
per-message status, pending approvals and callbacks from anywhere in the tree, the agent's
observable state typed from its Python models, and a `sendMessage` checked against each handler's
input model. Connection handling is written once, in the core. `examples/tictactoe/ui` is a whole
app built this way (`just play`).

## Where it lives

Four standalone npm packages under `packages/`, each with its own lockfile (like `ui/` and
`third-party-platforms/chat-server/`), so a consumer depends on the client without depending on
the console, and each can be published on its own later:

| package | what it is | toolchain |
| --- | --- | --- |
| `packages/codegen` — `@temporal-agent-harness/codegen` | `harness-codegen`: schema document → TypeScript | TypeScript 7, `node --test` |
| `packages/client` — `@temporal-agent-harness/client` | the framework-independent core, transport, projection, and the generated protocol types | TypeScript 7, `node --test` |
| `packages/svelte` — `@temporal-agent-harness/svelte` | the Svelte 5 binding | `svelte-package`, vitest in happy-dom, TypeScript 6 |
| `packages/react` — `@temporal-agent-harness/react` | the React binding (18.2+) | `tsc`, vitest with React Testing Library in happy-dom, TypeScript 7 |

There is no root workspace. Local packages depend on each other through `file:` links with
`install-links=false` (in each package's `.npmrc`), so they are symlinks that build against the
working tree; a copy would leave out the linked package's gitignored `dist/`. The binding needs
TypeScript 6 because `svelte-check` and `svelte-package` drive the compiler through its
JavaScript API, which TypeScript 7 does not ship.

## Inspiration: `@ai-sdk/svelte`

The AI SDK is inspiration only — there is no integration and no `ChatTransport` adapter. Its model
(the client owns `messages[]` and re-POSTs history per request) is the inverse of ours (the
workflow owns the conversation; the stream is a durable, offset-addressed log), so an adapter
would be lossy exactly where the harness is richer: queued/joined dispositions, auto-approval
evaluations, observable state, subagents.

What we took from the shape of `@ai-sdk/svelte@5`:

- **The binding is small.** All behaviour lives in a framework-independent core
  (`AbstractChat` there, `AgentSessionCore` here); a framework plugs in by implementing a state
  interface (`ChatState` there, `SessionState` here) whose fields it makes reactive.
- **A class, not a hook.** `new AgentSession({...})`, read through getters backed by Svelte state.
- **Batched reactive writes.** Frames update private working state; the core writes to the
  reactive state once per flush.
- **A keyed store through context**, so components that construct the same session share one
  state.

Where we deliberately differ:

- **Updates land on any message, not only the last.** A refcounted turn can stream two messages at
  once, a queued message sits earlier in the list, and an approval can resolve on an older message.
  The projection finds every message by id and replaces it by index.
- **The connection is long-lived.** An AI SDK chat has nothing open between requests; we hold an
  `attach`. Sharing one attach per session is a correctness concern (the update cap), not polish.
- **The raw frame log is exposed alongside messages**, because the console is an inspector
  (replay, graph, cost, audit panels), not only a chat.
- **One message per inbound message**, not a user message plus an assistant message.
- **Left out:** a single `status` enum (we have a connection status, agent-level `idle | busy`, and
  per-message status), a writable `messages` setter, `regenerate`, `stop` as "abort the fetch"
  (detaching doesn't stop an agent; `closeSession()` does), and `sendAutomaticallyWhen` (the
  workflow continues its own loop after an approval or callback).

## As built

### State is declared on the class

```python
@agent.defn(name="TicTacToeAgent")
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

- `agent.state(...)` (`harness/state/decl.py`) returns a `StateDecl[T]` descriptor. `__set_name__`
  takes the state id from the attribute name, so the id can't drift from the attribute. An
  overloaded `__get__` returns the declaration when read from the class (what codegen reads) and
  the instance's `StateRef[T]` when read from an instance; pyright types both.
- Refs are stored **on the instance, never on the descriptor.** A class attribute is shared by every
  workflow instance on a worker; holding the ref on the descriptor would leak one workflow's board
  into another. `__set__` raises, so `self.board = Board()` cannot shadow the declared state.
- Bad declarations fail when the class body runs: a non-`HarnessState` type, a type with required
  fields and no `initial=`, one declaration under two names. `@agent.defn` records nothing extra —
  the descriptor validates itself, and a recorded copy would be inherited by subclasses and go
  stale. `declared_states(cls)` reflects over the MRO in declaration order.
- The `AgentWorkflowRunner` constructor gets the instance from the stack walk it already did
  (`_enclosing_workflow_instance`) and publishes every declared state's snapshot at the end of
  construction, in declaration order. Its signature didn't change.
- A ref read or mutated in `__init__` before the runner exists is created lazily; whatever it
  committed then was published to no one, so it folds into the first snapshot at version 0
  (`StateRef._attach`).
- `runner.state()` is gone. Without that, the generated schema could not claim to list every
  state an agent publishes. The tests' `StateHost().state(...)` stays as the unit-test primitive;
  it publishes to no agent stream.

**Where it must be written.** Calling `agent.state(...)` anywhere is harmless, but it only does
anything as a class attribute. The wrong placement fails at the line that uses it, as a type error:

```python
def __init__(self, config):
    self.board = agent.state(game.Board)   # wrong place
...
with self.board.mutate() as d:             # pyright: "StateDecl[Board]" has no attribute "mutate"
```

**One hazard is plain Python.** A state and a handler method with the same name overwrite each
other in the class namespace — whichever comes last wins — and nothing is left to detect it.

`HarnessState` is already strict, which is what makes it generate cleanly: `extra="forbid"` becomes
`additionalProperties: false`, and `StateSchemaError` rejects fields the harness can't own at
class definition.

### Tool inputs and outputs stay opaque JSON

Tool parts carry `input: Record<string, unknown>` and `output: string | null`. Clients rarely need
tool I/O typed, and the one case that matters — callback tools — already requires the client
author to implement the contract, so they validate it where they implement it (zod or similar).
This keeps `run_tool` unchanged, needs no tool declarations anywhere, and leaves runtime-configured
tools (a user adding or removing an MCP server through a handler) entirely unconstrained by the
harness. See **Rejected** for the tool-typing design this replaces.

### Schemas from Python

`harness/agent_schema.py`:

- **`agent_schema(cls)`** — handlers and declared states, printed by
  `temporal-agent-harness schema module:Class`:

  ```
  { agent, source, handlers: { name: { description, mid_turn, input, output } }, states: { id: ref }, $defs }
  ```

  One `models_json_schema` call, so every model shares one `$defs`. Handler inputs use
  `mode="validation"` (a field with a default may be omitted going in); handler outputs and states
  use `mode="serialization"` with **every model field required**, via a small `GenerateJsonSchema`
  subclass — pydantic's default marks defaulted fields optional even in serialization mode, which
  would type a reply or state document as possibly missing fields it always carries. A model used
  both ways whose schemas differ appears as `Name-Input` / `Name-Output`. Two distinct models that
  share a class name raise, naming both — including the case where pydantic would silently call
  them `Name-Input` / `Name-Output` because they were used in different modes.
- **`protocol_schema()`** — the event stream's wire types, printed by
  `temporal-agent-harness schema --protocol`: the `AgentEvent` envelope, the payloads in
  `AgentStreamItem` order, and `$defs`, all in serialization mode.

Both are pure reflection: no workflow is started and no Temporal connection is needed. Field
descriptions must be `Field(description=...)`; attribute docstrings reach the schema only on a
model that sets `use_attribute_docstrings=True`.

### TypeScript from the schemas

`harness-codegen <schema.json | -> [-o file] [--name T] [--protocol]` runs `json-schema-to-typescript`
over `$defs` and appends what no off-the-shelf tool produces:

- **For an agent**, a mapping type named after its workflow type (`--name` overrides it):

  ```ts
  export interface TicTacToeAgent {
    handlers: {
      /** Reset the board and start a new game. ... */
      new_game: { input: NewGame; output: TextReply };
      /** Play your mark on an empty cell (1-9, left-to-right, top-to-bottom). ... */
      play: { input: PlayMove; output: TextReply };
    };
    states: { board: Board };
  }
  ```

- **For the protocol**, `AgentStreamItem` (the union of payloads) and
  `AgentEventType = AgentStreamItem["type"]`.

Before handing the schema over, the generator drops property-level `title`s and emits every `$ref`
as the referenced type's name (via `tsType`, keeping its description); otherwise
`json-schema-to-typescript` mints a named type per title and a duplicate type (`Foo1`) per `$ref`
that carries a sibling description. Min/max-style constraints become JSDoc tags, Sphinx roles in
docstrings become Markdown, and `Echo-Input` becomes `EchoInput`, rejecting two names that would
generate one. `Literal` aliases are inlined by pydantic, so a field reads `"X" | "O"` rather than
`Mark`.

**Where generated files live, and what keeps them current.** Each example that ships a typed client
owns a `codegen-client-sdk` recipe in its own justfile and commits the output
(`examples/tictactoe/client_sdk/TicTacToeAgent.ts`); there is no top-level codegen recipe. The
protocol types are committed as `packages/client/src/protocol.ts` (`npm run generate:protocol`
there). The `client-sdk` CI workflow regenerates both — for every example whose justfile defines
the recipe — and fails on any difference.

### The client core

`AgentSessionCore<A extends AgentSchema = UntypedAgent>` (`packages/client/src/session.ts`) owns one
session:

- **The connection.** Attach from offset 0. The server ends an attach once the agent is idle and
  caught up; a stream that errors, or ends with a turn still open, is retried with backoff
  (`[500, 1000, 2000, 4000, 8000, 8000, 8000]` ms, reset whenever it delivers) and resumes from the
  last `resume_offset`. A clean end with the workflow closed is `closed`. A clean end with the agent
  idle is `idle`: the core then **polls `agent_status`** (5 s by default) and re-attaches only when
  it shows new work — how a message sent from another client gets noticed without holding a
  parked poll open. A send wakes a sleeping retry or an idle wait at once. Frames a resume
  redelivers are dropped by key. Submits go out one at a time, in order, and a stream is ensured
  only after the submit returns, so it cannot end before the message is admitted.
- **The projection.** Incremental: each frame updates only the message, part or state document it
  is about, found by id, so a replay costs O(frames). A changed part is replaced, never edited, so
  a published object never changes afterwards. However many frames arrive between flushes, the
  state is written once per flush — a 1,500-frame catch-up in one flush is one write.
- **Every agent in the tree.** The merged stream carries the root agent's events and, recursively,
  every subagent's, each stamped with its tree-unique `agent_id`. Each agent gets its own
  projection, and its own published view: messages, states, `agentStatus`, `parentId`, `agentKey`,
  `workflowId`, `lifecycle` (`stopped` once its parent stops it), and `unavailable` (why its own
  events could not be read). A parent's `subagent` part carries the `messageId` of the message the
  child ran for that turn, so a view can render the child's work in place.
- **Actions.** `sendMessage(handler, payload)` (typed from `A`), `respondToApproval`,
  `provideToolOutput` / `provideToolError`, `closeSession`, `dismiss`, `clearError`. None of them
  reject; failures set `error` (and `onError`). Approvals and callback results are sent to the
  workflow of **the agent that made the call**, found by `tool_id` — a subagent's gates are its
  own, not its parent's.
- **Callbacks.** `callbackTools` maps tool names to handlers. A handler is called once per call
  to its tool still waiting at a flush, anywhere in the tree, including one that was waiting
  before this client attached; a callback already resolved earlier in the same catch-up is not
  fulfilled again. A tool with no entry is left alone for a person to answer, so one session can
  fulfil some callback tools itself and show the rest in `pendingCallbacks`, which leaves out the
  ones `callbackTools` answers. `onFinish` fires only for
  messages this client sent.

Reactivity comes in through `SessionState`:

```ts
interface SessionState<A extends AgentSchema> {
  connection: "stopped" | "connecting" | "live" | "idle" | "reconnecting" | "closed" | "error";
  error: Error | undefined;
  rootAgentId: string | null;
  agents: Readonly<Record<string, AgentView>>;     // rootView(state) reads the root, typed AgentView<A>
  pending: readonly HarnessMessage<A>[];
  frames: readonly AgentSseFrame[];
  updateAgents(updates: ReadonlyArray<readonly [agentId: string, AgentUpdate]>): void;
  appendFrames(frames: readonly AgentSseFrame[]): void;
}
```

The core writes once per flush (the next animation frame by default). `applyAgentUpdate` applies an
update, so a binding only chooses where to keep the result. `PlainSessionState` is the
framework-free implementation, with a `subscribe` listener, for scripts and tests. `HttpTransport`
implements `SessionTransport` over the harness web API.

### The message model

One `HarnessMessage` per inbound message, narrowed on `handler` to that handler's `input` and
`output` types:

- `id` is the `message_id`; `status` is `sending → accepted → running → done | error`, and
  `disposition` says whether it opened a turn, joined the open one, or queued behind it.
- `parts`, in stream order, are everything the agent did in answer: `model_interaction` per model
  call (with its usage), `reply_delta` for the streamed reply text, `thought_summary`, a
  `text_annotation` per annotation, `tool` per tool call, `subagent` per subagent turn. Each part
  is named for the protocol event it comes from. The reply is `output`, typed by the handler's
  output model.
- Tool parts are keyed by `tool_id` and move through
  `requested → [awaiting_approval ⇄ evaluating → approved | denied] → running → [awaiting_callback] → done | failed`.
  `evaluating` (an automatic evaluator is deciding) is distinct from `awaiting_approval` (a person
  must act); every evaluation stays on the part for audit views. A tool that starts without a
  request (a built-in one) and a resolution published with no `message_id` (a policy cascade) are
  both found by `tool_id`.
- Messages this client sent that the server has not admitted yet are in `pending`, not `messages`:
  the stream can admit a message before its submit returns, and a placeholder in `messages` would
  then have to be deleted from the middle of the list. A failed send stays in `pending` with its
  error until `dismiss`ed.
- `state_snapshot` / `state_patch` are not messages; they fold into each agent's `states`. A patch
  that arrives after a version gap, or with no snapshot (an attach from a later offset), stops that
  document at its last good version instead of guessing.

### The Svelte binding

`packages/svelte`:

- **`SvelteSessionState`** — `$state.raw` fields, each flush assigning new objects. The core always
  hands over whole new objects, so deep proxies would buy nothing and make replaying a long
  session expensive.
- **`AgentSession<A>`** — `messages`, `states` and `agentStatus` (the root's); `agents` and
  `agent<B>(id)` for every agent, typed with a child's own generated type when given one;
  `pending`, `connection`, `error`, `frames`; `pendingApprovals` and `pendingCallbacks` collected
  from the whole tree; and the core's actions.
- **The connection follows observation**, via `createSubscriber`: the first effect or markup to
  read any field opens the stream, and it closes (in a microtask) once nothing reads it, so
  unmounting the last view releases its update slot with no teardown code.
- **Getter options** — a `get sessionId()` moves the connection when its value changes.
- **`createHarnessContext()`** puts a `SessionStore` in context: every `AgentSession` below it with
  the same session id shares one core, one state and one connection, reference-counted across
  readers. The first to open a session decides its options (`callbackTools`, transport, …).

Its tests run under Svelte's client runtime in happy-dom: in Vitest's Node environment modules are
transformed as SSR, where effects never run.

### The React binding

`packages/react`:

- **`ReactSessionState`** — an immutable snapshot of the session's fields, replaced on every write,
  with `subscribe` and `getSnapshot` for `useSyncExternalStore`. The snapshot's identity changes
  exactly when a field does, which is what that hook requires; the fields inside are the core's own
  objects, never copied.
- **`useAgentSession<A>(options)`** returns the same fields and actions as the Svelte
  `AgentSession`, as of this render. `pendingApprovals` and `pendingCallbacks` are computed only
  when a render reads them. Both bindings get them from the core's `waitingCalls`.
- **The connection follows mounting**: an effect acquires the session and its cleanup releases it.
  A release takes effect a microtask later, and an acquire before then cancels it, so StrictMode's
  mount → unmount → remount in development keeps the one connection instead of reconnecting. A
  changed `sessionId` is a different session, so the effect moves the hold to it.
- **`<HarnessProvider>`** is `createHarnessContext()`'s counterpart: a `SessionStore` in context,
  shared by every `useAgentSession` below it. Without one, each component has a store of its own.
- **Server rendering** returns the empty snapshot, and effects never run on the server, so nothing
  connects until the client hydrates.

Its tests use React Testing Library in happy-dom, including StrictMode and `renderToString`, and a
`TicTacToeBoard.tsx` that `tsc` type-checks against the example's generated types.

### The tic-tac-toe app

`examples/tictactoe/ui` replaces the old `play.html`. It is a single Svelte component, written only
against the binding and the generated `TicTacToeAgent` type: the board is `agent.states.board`,
moves are `agent.sendMessage("play", { cell })`, the last TypeSafe judgment is read off
`agent.messages`, the ledger off `agent.frames`, and gated calls get approve / deny buttons from
`agent.pendingApprovals`. It has no build step: an import map and a service worker let the browser
compile it. CI type-checks it against the committed generated types, so a model change that breaks
it fails there.

## Phases

| phase | what | commit |
| --- | --- | --- |
| 1 | `agent.state(...)` declared on the class; `runner.state()` removed | `7f01f3b` |
| 2 | `agent_schema(cls)` and `temporal-agent-harness schema` | `94bb9b2` |
| 3 | `packages/codegen` and the per-example `codegen-client-sdk` recipe | `74e868a` |
| 4 | protocol types generated from `events.py`; the UI's event types derived from them | `53c14ff` |
| 5 | `packages/client` (core, projection, per-agent views, transport) | `8078b7e` |
| 5 | `packages/svelte` (the binding) | `c1bf85c` |
| 5 | `packages/react` (the React binding) | `f443bf2` |
| — | `examples/tictactoe/ui` on the binding, replacing `play.html` | `c26da1e` |
| 6 | the console on the client | not started |

Each phase's work was verified against a local stack (a Temporal dev server, the session manager,
the web server and the example workers): the core drove tic-tac-toe sessions and a model-free
parent driving a real Monty subagent end to end over HTTP, and the tic-tac-toe app was played in a
headless browser.

### Phase 6 — the console

- **Build:** `ui/` depends on `packages/client` and `packages/svelte` through `file:` links;
  `AgentChatPanel` and `TranscriptPanel` move onto an untyped `AgentSession`, with
  `AgentRunController` keeping replay, graph and cost on `session.frames`. `ui/src/lib/api/types.ts`
  imports the SSE frame types from `packages/client/src/frames.ts` rather than keeping its own copy
  of them.
- **Needs:** a `pnpm install` on the machine that owns `ui/node_modules`.
- **Done when** the console behaves as before on the client, and its own connection handling is
  gone from `AgentRunController`.

## Open decisions

- **Sending to a subagent directly.** The client only sends to the root. A child turn started by
  someone other than its parent is not part of the parent's merged stream (the merge only admits a
  child's turns its parent asked for), so showing one would need a second session on the child's
  own workflow id. The old console does this by attaching to the child's stream separately.
- **Fine-grained reactivity per agent.** `agents` is replaced as a whole each flush, so a reader of
  one agent re-runs when any agent changes (the arrays it reads are unchanged, so the DOM is not
  touched). Worth revisiting if a large tree makes it measurable.
- **Publishing.** The packages are `private`. Publishing needs a decision on the npm scope
  (`@temporal-agent-harness/*` needs that org; Temporal publishes under `@temporalio`), `files` /
  `exports` fields, and whether `harness-codegen` stays its own package or becomes a bin of the
  client package.
- **A root workspace.** With four packages plus `ui/` and the chat-server, a root npm or pnpm
  workspace may now pay for itself; it would replace the per-package lockfiles and `file:` links.
- **`event_offset` and `replay` on the SSE envelope.** The client and the UI type them as optional,
  but the current server never sends them.
- **Relation to `rendering-agent-state.md`.** That design puts a runtime `value_schema` (with
  `x-harness-display` hints) on `AgentStateSnapshot`. Both should come from the same
  `agent_schema` call; unknown `x-` keys pass through `json-schema-to-typescript`, so they don't
  conflict.

## Risks

- **The stack walk.** The runner finds its agent instance by walking to the `@workflow.init` frame's
  `self`. A runner built somewhere that frame isn't on the stack would publish no declared state.
- **Stale types across deploys.** A session started on an older build can replay snapshots with an
  older shape. Accepted for now; a schema hash on `AgentStateSnapshot` would detect it.
- **Platform-bound `node_modules`.** TypeScript 7's compiler and Vite 8's bundler are native
  binaries, so a `node_modules` installed on one OS doesn't run on another. The Node recipes run
  `npm ci` every time so a checkout shared between machines repairs itself.

## Rejected, with reasoning

- **An AI SDK integration or `ChatTransport` adapter.** Lossy where the harness is richer (see
  **Inspiration**), and the approval flow doesn't fit: useChat expects to re-POST after an
  approval, while the workflow continues on its own.
- **Prototyping the client inside `ui/`.** Consumers would have had to depend on the console to get
  a client, and the codegen tool would have lived in an app it has nothing to do with.
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
- **A user message plus an assistant message per inbound message**, as the AI SDK has. The harness
  already pairs a request with everything it caused by `message_id`; splitting them would only make
  every consumer join them back up.
- **Re-attaching an idle session on a backoff until a budget runs out**, as the console's
  controller does. It stops listening after about 30 s and misses work another client starts
  later; polling the cheap `agent_status` query does neither.
