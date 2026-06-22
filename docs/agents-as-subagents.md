# Agents as Subagents (drop-in subagent toolset)

**Status:** 🚧 In progress. **A ✅ · C ✅ · D code ✅** (the toolset generator, the runner-side
wrapper, AND the `run_subagent_turn` activity are all shipped + unit-tested) — what remains in
**D is the example parent wiring + an end-to-end test**. **B ⏸ deferred** (idempotency hardening),
**E** partially done (unit tests landed with A/C/D; the e2e integration test is the open item).
This doc is the source of truth for the design and the per-workstream progress tracker; since the
mechanism is shipped code, the design is best read directly from `harness/subagent_toolset.py`,
`harness/subagent_activities.py`, `harness/agent_protocol/subagent_interface.py`, the subagent
section of `harness/agent_workflow.py`, and the tests in `harness/test_subagents.py`.
**Scope (delivered):** `harness/` (the `subagent_toolset` generator + the `run_subagent_turn`
activity + the runner-side registry/gate/start-stop-turn methods + handler-dispatch message
handling + structured rejection details), `harness/agent_client.py` (split into the private
`_submit_message` / `_stream_turn` halves the activity reuses), and the example agents (migrated
to `@agent.accepts` handlers). **Remaining:** wire a real parent→subagent demo + e2e test (D).
**Last updated:** 2026-06-17

---

## Goal

Provide a **drop-in harness utility** that converts a *specific*, statically-chosen agent
definition into a **toolset** the parent agent can use to drive it as a **subagent**. The
agent developer picks a concrete agent to wire up (a parent may wire several different
agent types, each as its own namespaced toolset), so the parent must **not** spend tokens
discovering the subagent's capabilities at runtime — capabilities are resolved
**statically, without ever starting the subagent workflow**.

The harness already standardizes every agent's interface — a uniform start (one
`AgentConfig`), a uniform front door (the `send_agent_message` update, renamed from
`user_input`), a discovery query (`agent_interface`, renamed from
`accepted_message_types`), turn-events stream, and a `close` signal. This feature is the
adapter that lets one harness agent drive another through that same standardized surface.

### Generated toolset (per wired subagent, namespaced to that agent)

- `start_<key>` — start the subagent as a child workflow (`start_child_workflow` with its
  registered `@workflow.defn` type name + task queue + an `AgentConfig`). Stash the child
  `workflow_id` on parent-side state.
- `stop_<key>` — send the harness `close` signal (and/or cancel) to the child; clear state.
- `send_<function>` — **one tool per `@agent.accepts` handler** the subagent declares. The
  tool name is the handler function name; its parameters schema is the handler's input
  `AgentMessage` JSON schema, its result schema is the handler's output-model JSON schema,
  and its description is the handler's docstring — all read from `agent_interface`. This is
  the two-activity tool (Workstream C).

---

## Key prior-art / anchors in the codebase

- `harness/agent_workflow.py` — `AgentWorkflowRunner`, `Turn`/`turns()`/`AgentRunContext`,
  `@agent.defn` contract, `tool_defn` / `activity_tool_defn`, `run_tool`, `Injected[...]`,
  approval gate. Workstream A **replaces** `turns()`/`Turn` + the dev's `match` loop with
  handler-dispatch (`@agent.accepts` methods + `runner.run(self)`).
- `harness/_runner_builder.py` — was the fluent, type-accumulating builder. Workstream A
  **deletes this module entirely** (along with `Generic[M]`, `add_accepted_message`, and the
  sentinel-key construction guard): with `M`-accumulation gone, the builder was pure
  ceremony. The runner is now constructed directly —
  `AgentWorkflowRunner(config, stream=..., approval_policy_default=..., enable_message_queuing_default=False, custom_approval_fallback=None)` — with the config-vs-default resolution folded into `__init__`
  (`stream` + `approval_policy_default` are required kwargs). Accepted messages are
  discovered from `@agent.accepts` handler signatures.
- `harness/agent_client.py` — `send_message()` (the one PUBLIC turn driver) does update **+**
  stream-consume in one call, composing two **private** halves: `_submit_message()` (phase 1)
  and `_stream_turn()` (phase 2). These are in-package primitives the harness reuses (e.g. the
  subagent activity); B adds idempotency to the `_submit_message` path.
- `harness/agent_protocol/agent_interface.py` — `AgentConfig`, `AgentMessage`,
  `UserInput`, `UserInputResult`, the `user_input` validator's `StaleTurn` / `AgentBusy` /
  `MalformedMessage` rejections. Workstream A: the `user_input` update → **`send_agent_message`**;
  its payload → an `AgentMessage{type, payload}` **envelope** (`type` names the target
  handler; `payload` is that handler's input-model JSON) — note `AgentMessage` is
  **repurposed** from "input base class w/ discriminator" to "the wire envelope". The old
  discriminator requirement and `_discriminator_value` are removed. `AcceptedMessageTypes`
  is **replaced** by `list[AcceptedFunction]` under the renamed `agent_interface` query.
- `harness/agent_protocol/events.py` — `AgentReply` (`reply`), `TurnEnded` (`turn_end`).
  `AgentReply.output: dict[str, Any]` carries the handler's **return value**
  (`result.model_dump(mode="json")`, no manual publish). A `dict` round-trips trivially on
  the shared stream union; a consumer that knows the expected type (from `agent_interface`)
  re-validates it — i.e. boundary validation, rather than making `AgentEvent` generic over
  every agent's output union.
- `server/session_manager.py` — agent-agnostic session manager: starts ANY registered agent by
  type name as a child workflow. The `start_<key>` tool mirrors this.

---

## ⚠️ Cross-cutting decisions still OPEN (resolve before/within the relevant workstream)

1. **[LOCKED] Handler-dispatch + name-routed tool-call envelope (`@agent.accepts`).** See
   Workstream A. Each accepted message is an `async` method `(self, msg: InputModel) ->
   OutputModel` marked `@agent.accepts`; the harness discovers them and publishes the
   **return value** as the turn's reply. The wire is a tool-call envelope —
   `AgentMessage{type, payload}` on the renamed `send_agent_message` update — where `type`
   **names the target handler** (its function name) and `payload` is that handler's input
   model JSON. Routing is **by name, not by a discriminator** — so multiple handlers may
   accept the *same* input model. Input/output are both (non-scalar) pydantic models;
   input no longer needs an `AgentMessage` base or a discriminator. Handler, input model,
   and output model all require docstrings. This supersedes both the `AgentSpec`/`RepliesWith`
   line (phantom output types, mismatchable `reply()`) **and** the earlier
   discriminator-routed variant (which forbade two handlers sharing an input type). Static
   discovery is pure reflection over the handler signatures — no workflow started — and
   input→output association is checked by the plain return type. Shipped + tested in
   `harness/test_runner_builder.py` (incl. two handlers sharing one input model).
2. **[RESOLVED — 2026-06-15] Per-subagent FIFO gate (caller-side serialization), NOT eager
   turn-number prediction.** A parent may issue several `send_<function>` calls to the *same*
   subagent in one model turn and `asyncio.gather` them. We serialize those **on the caller
   side** with a **per-subagent FIFO gate** in the registry (Decision #5), rather than
   predicting turn numbers and leaning on the child's queue. The `send_<function>` tool:
   (a) emits `tool_started` first — naturally, since the `tool_defn` wrapper publishes it
   before the body runs, so a concurrent call that then blocks on the gate still correctly
   shows as "started"; (b) acquires the gate (**FIFO admission preserves the model's call
   order** — the synchronous prefix of each gathered coroutine runs in order); (c) *after*
   acquiring, reads the now-exact `next_expected_turn` + `last_consumed_offset`, runs the
   single `run_subagent_turn` activity, then on completion advances `next_expected_turn` and
   stores `consumed_offset`, and releases.

   **Why this beats eager turn-number prediction.** A subagent processes turns sequentially
   regardless, so caller-side gating loses no throughput; and it dissolves two problems that
   prediction has: (i) no update **arrival-order race** (only one activity per subagent is
   ever in flight — nothing to race at the child's `expected_turn` check), and (ii) **no
   counter skew** (the turn number is read *after* the wait, always exact — no prediction, no
   reconciliation; this is why the old "reconcile on rejection" question is now moot). Turn
   counter advances on completion: on success `next_expected_turn = result.turn_number + 1` +
   `last_consumed_offset = result.consumed_offset`; on a turn that was *accepted but errored*
   (`SubagentTurnError`/`SubagentNoReply`) still advance the turn counter by one (the child
   consumed that turn) and surface `is_error`; on a pre-acceptance rejection (abnormal under
   the gate — e.g. an external driver) advance nothing and surface `is_error`.

   **Consequence (explicit):** this **bypasses the child's own message-queuing mechanism** for
   parent-driven turns — the parent serializes caller-side instead, so `AgentBusy` /
   `is_message_queuing_enabled` never surface to the parent. A deliberate reversal of the
   earlier "expose the child's queuing model" intent, traded for race-freedom at no throughput
   cost. Gates are per-subagent, so calls to *different* subagents still run concurrently.
3. **[OPEN — Workstream B, deferred] Hash-retention scope** — retain only the last K turns'
   `turn_number → hash` (dedupe window is the activity-retry window, ~seconds), vs.
   unbounded. Leaning bounded-K. Only relevant once idempotency is picked up.
4. **[OPEN — Workstream B, deferred · verify] Reading `ApplicationError.details` off a failed
   update** — confirm the Python SDK exposes `cause.details` on
   `WorkflowUpdateFailedError.cause`. Fallback: encode structured info in the message string.
5. **[RESOLVED — 2026-06-15] Shared per-session state home → nested in the runner, reached
   via the `_CURRENT_RUNNER` contextvar.** NOT a holder object, NOT `Injected[...]`, and
   **no `has_self`** (the `has_self` de-risk is therefore moot — we never take that path).
   The subagent registry is **keyed by a short (~6 hex char) `handle`** (per entry: `handle` +
   the real child `workflow_id` + `agent_key` + `next_expected_turn` + `last_consumed_offset` +
   a **per-subagent FIFO gate** — see Decision #2), nested **inside `_WorkflowStatus`**. The
   **model only ever sees the short `handle`** — never the long `workflow_id` (cheaper for it to
   reproduce and harder to get wrong); the workflow-side resolves `handle` → `workflow_id`
   before calling the activity. The generated `start_/stop_/send_` tools are inline `tool_defn`
   closures (over `agent_key` + `AcceptedFunction`); at call time they resolve the live runner
   from the already-set `_CURRENT_RUNNER` contextvar (`run_tool` parks it for every tool call)
   and invoke public runner methods that mutate the nested state deterministically in-workflow.
   `start_<key>` **returns the `handle`** to the model; `send_/stop_` take a `handle` arg.
6. **[LOCKED — 2026-06-15] STREAM ISOLATION: a subagent's stream is NEVER mirrored onto a
   parent agent's stream.** Not reply deltas, not tool events, *nothing*. The
   `run_subagent_turn` activity consumes the **child's** stream purely to capture the reply
   and detect `turn_end`; it mirrors **none of the child's content** back to the parent. (It
   does publish ONE parent-stream marker of its own — `subagent_message_sent`, below — but that
   is the parent agent's own record of "I messaged a subagent," not any of the child's events.)
   Each agent's stream stays a clean, single-agent record. Collecting multiple agents' streams for a UI is a
   **client-side** concern: `agent_client.py` will later learn to ad-hoc mount subagent
   workflow streams on demand so a UI can assemble them. **That client work is OUT OF SCOPE
   for now** — recorded here so the boundary is committed. What IS shipped to enable it: the
   parent emits **`subagent_started` / `subagent_stopped`** stream events (in `events.py`,
   alongside the tool events) carrying the subagent's `handle` + `agent_key` + real
   **`workflow_id`** — the `workflow_id` is precisely what a future client uses to mount/unmount
   the child's stream. So the lifecycle signal exists on the parent stream; only the
   *consuming* of the child streams is deferred. In addition, each **dispatch** to a subagent
   emits **`subagent_message_sent`** (2026-06-17) — same `handle` + `agent_key` + `workflow_id`
   plus the target `function` and the **`subagent_turn`** (the turn number ON THE CHILD —
   deliberately NOT named `turn_number`, since the enclosing `AgentEvent` envelope already carries
   the *parent's* `turn_number`; several dispatches in one parent turn share that envelope turn but
   get distinct `subagent_turn`s) — so a per-turn message to a specific subagent is distinguishable
   on the parent stream from any other tool call, and a consumer can correlate it with the matching
   turn on the (separately-mounted) child stream. Still no child *content* mirrored — only the
   dispatch marker. **Where it's published (2026-06-17):** the `run_subagent_turn` *activity*
   publishes it (via `publisher_from_activity`, which targets the activity's own parent workflow)
   at the moment it ACTUALLY sends the message to the child — not in-workflow at `execute_activity`
   dispatch time, which would claim "messaged" before the send happens (mirrors how tool activities
   publish `tool_start` from inside the activity). The activity's heartbeat-memo (`_TurnProgress`)
   dedups it: the publish fires only on a fresh send, never on a heartbeat-resume retry — so it
   inherits exactly the send's at-least-once-with-dedup guarantee. The in-workflow `run_subagent_turn`
   threads the parent's `TurnStreamContext` + `handle`/`agent_key` into the activity input for this.

---

## ✅ Resolved contradiction — `turn_end` on error

`events.py` (`TURN_END` / `TurnEnded`) docstrings used to claim turn_end was "emitted only
after a successful REPLY; a turn that ends in ERROR does not emit it" — contradicting the
implementation (and `agent_client.py`), which always emits it. **Canonical = always emit.**
Workstream A's `runner.run(self)` loop publishes `turn_end` in a `finally` for every turn
(after `AgentReply` on success, after `AgentError` on a raise), and the stale `events.py`
docstrings were corrected to match (2026-06-15).

---

# Workstreams

Each is intended to be separable and potentially worked in its own session. Status legend:
🧭 not started · 🚧 in progress · ✅ done · ⛔ blocked.

## Workstream A — Handler-dispatch message handling + static `agent_interface`
**Status:** ✅ DONE · **Blocks:** D (tool generation reads the handler signatures statically).
**Design:** ✅ LOCKED (`@agent.accepts` handler-dispatch). Shipped in `harness/agent_workflow.py`
+ `harness/agent_protocol/`; tests in `harness/test_runner_builder.py`.

> **Implemented (2026-06-15).** `@agent.accepts` + `_discover_handlers` + `agent_handlers`
> (`harness/agent_workflow.py`); `defn` stamps `__agent_handlers__` at import. Runner
> rewritten: name-routed `send_agent_message` validator (`StaleTurn`/`AgentBusy`/
> `UnknownFunction`/`MalformedMessage`, all with structured `details`), `agent_interface`
> query → `list[AcceptedFunction]`, and `runner.run(self)` turn loop (publishes the
> handler's return as the reply, `AgentError`+`turn_end` on raise, loop survives). Removed
> `Turn`/`AgentRunContext`/`turns()`/`start()`/`add_accepted_message`/the `M` type param —
> **and the whole builder**: `_runner_builder.py`, `Generic[M]`, and the sentinel-key guard
> are deleted; the runner is constructed directly,
> `AgentWorkflowRunner(config, stream=..., approval_policy_default=..., enable_message_queuing_default=False, custom_approval_fallback=None)`,
> with config-vs-default resolution in `__init__` (`stream` + `approval_policy_default`
> required). Protocol: `AgentMessage{type, payload, expected_turn}` envelope (the
> `expected_turn` is folded onto it; `UserInput` deleted), `AcceptedFunction`, `TextMessage`/
> `TextReply` built-ins (plain models, no discriminator), `AgentReply{output: dict}` (the
> handler's return model dumped to JSON — no `text`/`output_type`); `user_input`→
> `send_agent_message`, `accepted_message_types`→`agent_interface`. Client:
> `send_message(msg_type, payload, expected_turn, ...)` (flattened — callers don't import
> `AgentMessage`; builds the envelope internally), `get_agent_interface`. Migrated: QaAgent
> (`ask`/`slash`), MontyDynamicAgent (`run_script`), `server/app.py` (`/api/agent-interface`,
> hardcodes the `ask` text handler), `server/mcp/cli.py` (hardcodes `ask`), `chat.html`
> (slash UI + envelope) and `states.html` (agent-agnostic: renders `AgentReply.output` as
> raw JSON). All harness + Monty tests pass (40). pyflakes clean; no new pyright errors.
> **Not done (deferred to B):** message-hash dedupe / idempotent resend — the `details`
> payloads are in place, but `DuplicateMessage` + `submit_message` are Workstream B.

**Problem this solves.** We need (a) the subagent generator to read a subagent's accepted
messages — with input/output schemas + descriptions — **statically, no workflow started**,
and (b) the strongest possible static guarantee that an agent replies with the declared
type. The earlier `turns()`-iterator + manual `turn.publish(AgentReply(...))` model can't
give (b): the output type is detached from the input, so a wrong reply is a runtime
problem. (We explored an import-time `AgentSpec[M]` with a `RepliesWith[O]` phantom mixin;
it type-checked, but it was a phantom carrier and still allowed `turn.reply(WrongInput(),
WrongOutput())`-class mistakes. Superseded.)

**Locked design — typed message handlers the harness discovers and dispatches to.** The
dev declares one async method per accepted message; its **param type is the input**, its
**return type is the output**. No phantom types, no manual reply, no `match` loop — and
strictly less dev code (each former `case` body is just a method, plus a one-line `run`).

```python
@workflow.defn(name="QaAgent")
@agent.defn
class QaAgentWorkflow:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(   # discovers @agent.accepts methods on this class
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)          # internal loop: validate → route by type → publish return

    @agent.accepts
    async def on_text(self, msg: TextMessage) -> TextReply:
        """Answer a free-form question about the docs."""        # docstring → function description
        ...

    @agent.accepts
    async def slash(self, cmd: SlashCommand) -> SlashCommandResult:
        """Apply a slash command to the session."""
        ...
```

- **No `ctx` param.** Handlers reach the harness only through `self._runner`. Streaming
  during a turn (reply deltas, citations) is already the runner↔SDK integration
  (`google_genai_client(runner=self._runner)`), not manual publishing; tools go through
  `self._runner.run_tool(...)`; the runner tracks the active turn internally (dispatch is
  **sequential**), so it publishes against the right turn with nothing threaded. Add
  `runner.publish(event)` (targets the current turn) for the rare custom intermediate event.
- **The reply is the return value** — exactly one structured reply per turn (streaming
  *during* the turn is unaffected). The harness validates it against the declared output
  type and publishes it.
- **Wire = a name-routed tool-call envelope.** The `user_input` update is renamed
  **`send_agent_message`** and takes an `AgentMessage{type, payload}` (+ `expected_turn`).
  `type` names the target handler (its function name); `payload` is that handler's input
  model JSON. The validator resolves the handler by `type` and `model_validate`s `payload`
  into its input model (coerce or reject). **Routing is by name** — so two handlers may
  accept the same input model (e.g. `summarize(Document)` + `translate(Document)`); a
  discriminator could not express that. *(This is a deliberate wire change from the prior
  "no-change" stance — worth it for multi-handler-per-input and for matching the tool-call
  paradigm these handlers serve. TODO: revisit whether the input field should be named
  `name` rather than `type`, since it names a function, not a kind.)*

**Deliverables.**
- `@agent.accepts` marker decorator (returns the method unchanged; the dev's
  `(self, msg) -> Output` signature is fully preserved and type-checked).
- Discovery + validation at `build()` (over the enclosing class): each handler has exactly
  one non-`self` param whose type is a **pydantic model** (the input — no `AgentMessage`
  base / discriminator required); the return type is a **non-scalar pydantic model**;
  handler, input model, and output model all have **docstrings**; **function names unique**
  (the routing key + tool name). Build the `name → handler` map.
- `AgentMessage{type: str, payload: dict}` wire envelope + the renamed **`send_agent_message`**
  update (carrying the envelope + `expected_turn`). Validator: unknown `type` →
  `ApplicationError(type="UnknownFunction", details={name, known})`; bad `payload` shape →
  `MalformedMessage` (details). Dispatch coerces `payload` via the named handler's input model.
- `runner.run(self)` — the internal loop replacing `turns()`/`Turn`/`AgentRunContext`:
  await `send_agent_message`, resolve+coerce by name, `await` the handler, validate + publish
  the return as the typed reply, emit `turn_end` (and `AgentError` on raise — same semantics
  as the old `__aexit__`). Remove `turns()`, `Turn`, `AgentRunContext`.
- Delete `_runner_builder.py` (and `Generic[M]` / `add_accepted_message` / sentinel key);
  construct `AgentWorkflowRunner(config, stream=..., approval_policy_default=..., ...)`
  directly with the config-vs-default resolution in `__init__`.
- **`agent_interface`** (renamed from `accepted_message_types`): the query returns
  `list[AcceptedFunction]` — `{name (function), description (handler docstring),
  parameters (input-model JSON schema), output (output-model JSON schema)}`,
  Gemini-tool-shaped. This is the static discovery surface for the subagent generator (also
  readable by pure reflection over the class, no workflow started). Replaces
  `AcceptedMessageTypes` / `MessageTypeSchema` / `accepts_text`.
- Harness built-ins so the trivial case stays trivial: `TextMessage{text}` (a plain
  pydantic model — no base/discriminator) + `TextReply{text}`, used as a handler's
  input/output. There is **no** implicit bare-`str` channel anymore — free text is just
  `send_agent_message(type="on_text", payload={"text": ...})`.
- `AgentReply.output: dict[str, Any]` = the handler's return model dumped to JSON; harness
  publishes it. Consumers re-validate against the known output type (boundary validation —
  see Workstream C's consume side). `expected_turn` is carried **on the `AgentMessage`
  envelope** (no `UserInput` wrapper); the dedupe hash (Workstream B) must cover only
  `{type, payload}`, which `_render_message` already projects.
- Migrate the example agents to handlers: `QaAgent` (text + `SlashCommand` payloads),
  `MontyDynamicAgent` (`RunScript` payload). Migrate plain-text callers (`server/app.py`,
  `mcp/cli.py`) to send the `send_agent_message` envelope (`{type:"on_text", payload:{text}}`).

## Workstream B — Idempotent message submission (⏸ DEFERRED — do AFTER D verifies end-to-end)
**Status:** ⏸ deferred · **Sequence:** intentionally **last** — only after C+D have a real
parent→subagent relationship **working end-to-end** (see the dependency graph). This is a
**production-reliability** hardening pass (de-dupe on activity retry), not a
correctness-of-the-feature requirement, so it must not gate the working demo.

> **Already shipped in A (not part of this deferred work):** the *structured rejection
> `details`* on every `send_agent_message` rejection —
> `StaleTurn {expected_turn, next_turn}` · `AgentBusy {current_turn}` ·
> `UnknownFunction {name, known}` · `MalformedMessage {function, error}`. Those payloads are
> in place precisely so this workstream can add `DuplicateMessage` later without reworking
> the rejection surface.

**Why deferred.** The C/D send path (below) will, in its first cut, do a plain update — so a
**send-activity retry could double-submit** a turn. That is the *only* reliability gap this
workstream closes. It's a real production concern, but the feature is demonstrably correct
without it, so we verify the end-to-end flow first and harden second.

**Deliverables (when picked up).**
- **`DuplicateMessage` rejection** → `details={turn_id, turn_number, pending}` (the
  **original** acceptance), added alongside the A-shipped rejections.
- **Dedupe state.** `_WorkflowStatus` records per accepted message
  `turn_number → (content_hash, turn_id, pending)`, `content_hash = sha256(canonical_json({type, payload}))`
  (NOT the whole envelope — `expected_turn` is excluded; `_render_message` already projects
  `{type, payload}`), recorded at enqueue. Bounded to last K turns (Decision #3).
- **Validator branch.** If `expected_turn != next_turn` **but** a recorded turn at
  `expected_turn` has a matching hash → it's an activity-retry duplicate, not a stale
  client. **Still reject** (don't reprocess) but with `type="DuplicateMessage"` + the
  original handle in `details`.
- **Client:** `AgentClient._submit_message(...) -> AgentMessageReply` (private, already
  exists) — phase-1 only (the `execute_update`, no streaming). Parses `cause.type` +
  `cause.details` (Decision #4) into typed errors; maps
  `DuplicateMessage → return the original AgentMessageReply` (idempotent
  success). The C activity's **send step** switches from its plain update to this so a
  re-send returns `DuplicateMessage` instead of double-submitting.

## Workstream C — The harness subagent-turn activity (send + stream the reply, in one)
**Status:** ✅ DONE · **Depends on:** A only (the `turn_end`-always docstring fix
already landed in A).

> **Shipped (2026-06-17).** Implemented exactly as the locked shape below:
> `harness/subagent_activities.py` (`SubagentActivities(client).run_subagent_turn` + the
> `_TurnProgress` heartbeat memo + the steady-interval `_auto_heartbeat`), the sandbox-safe
> contract in `harness/agent_protocol/subagent_interface.py` (`RUN_SUBAGENT_TURN_ACTIVITY`,
> `RunSubagentTurnInput`, `SubagentTurnResult`, the default timeouts), and the
> `agent_client.py` split into `_submit_message` / `_stream_turn` (both reused by the activity).
> The only deferred piece is closing the residual double-submit window — that's Workstream B.

> **Locked shape (2026-06-15).** The activity is a method of a small **class that closes
> over a Temporal `Client`** (`SubagentActivities(client).run_subagent_turn`), NOT a
> module-level `_TEMPORAL_CLIENT` global. The worker registers the **bound method** as the
> activity (`activities=[SubagentActivities(client).run_subagent_turn]`); a future harness
> **worker plugin** will instantiate it from the worker's client automatically (deferred —
> manual wiring for now). The closed-over client is used for BOTH the `send_agent_message`
> update and the stream subscribe against the *child* — but **not by re-implementing them**:
> the activity drives the child through the same `AgentClient` front door a human/UI uses.
> `agent_client.py` was refactored into two **private** composable halves of `send_message` —
> `_submit_message(...)` (phase 1: the update + `StaleTurn`/`AgentBusy` mapping) and
> `_stream_turn(turn_id=…, from_offset=…, timeout=…)` (phase 2: the turn-id/error/turn_end
> reduce loop, `timeout: float | None` so `None` = wait indefinitely). `send_message` (the one
> PUBLIC way to drive a turn) is now just `_submit_message` + `_stream_turn`, and the activity
> composes the same two primitives (it can, being in the harness package) — skipping
> `_submit_message` on a heartbeat-memo resume. So the front-door semantics live in ONE place;
> the activity only adds the dedup memo, the interval auto-heartbeat, and result capture.
> Input/output are pydantic models:
> `RunSubagentTurnInput{child_workflow_id, type, payload, expected_turn, from_offset, handle,
> agent_key, parent_stream_context}` → `SubagentTurnResult{output: dict, turn_id, turn_number,
> consumed_offset}`. **Per Decision #6 the activity mirrors NONE of the child's stream content
> onto the parent** — it only reads the child's; the one thing it does publish onto the parent
> stream is its own `subagent_message_sent` dispatch marker (see Decision #6), which is why the
> input carries the parent's `parent_stream_context` + `handle`/`agent_key`.
>
> **`from_offset` / `consumed_offset` — caller-owned, not fetched here.** The activity NEVER
> fetches the live stream head. The starting offset is an INPUT (`from_offset`), threaded by
> the wrapping in-workflow tool from its local per-subagent state; the activity returns the
> ending offset (`consumed_offset`) and the wrapper stores it for the next turn. The offset is
> a PERFORMANCE HINT only — `_stream_turn` filters to the turn's `turn_id`, so a stale/smaller
> offset merely replays a few seen events, and it can never be too large (the next turn's
> events always follow the prior `turn_end`). This is what lets the wrapper be a deterministic
> in-workflow loop with no I/O for offset discovery.
>
> **Module layering (done).** The activity NAME, the `RunSubagentTurnInput`/
> `SubagentTurnResult` models, and the recommended timeouts live in the sandbox-safe
> `harness/agent_protocol/subagent_interface.py` (stdlib + pydantic only; re-exported flat from
> the `agent_protocol` package), which the workflow-side runner imports; `subagent_activities.py`
> keeps only `SubagentActivities` + the client logic. They must be sandbox-safe because the
> runner (in the workflow sandbox) builds + dispatches the activity — so they can NOT live in
> `subagent_activities.py`, which imports the client/stream-client. Kept as a sibling of
> `agent_interface.py`/`events.py` (a distinct *parent-workflow ↔ harness-activity* contract,
> not the *agent ↔ client* one).
>
> **NO stream-consume timeout + interval auto-heartbeat (locked 2026-06-15).** The activity
> NEVER caps how long it waits for the subagent's terminal reply — a subagent may take
> arbitrarily long and its stream emits at unpredictable cadences. Instead of heartbeating
> off (sparse) stream events, a background task heartbeats at a STEADY `heartbeat_timeout / 2`
> interval (mirroring `temporalio.contrib.openai_agents`' `_auto_heartbeater`), carrying the
> live dedup memo (`_TurnProgress`, mutated in place as the consume offset advances — never an
> empty heartbeat that would clobber the "already sent?" guard). So **liveness = the short,
> predictable `heartbeat_timeout`**, not a guess at turn duration. **Timeout config is the
> caller's (D) job:** Temporal *requires* a `start_to_close_timeout` **or**
> `schedule_to_close_timeout` (enforced in the SDK — `heartbeat_timeout` alone is rejected),
> so the toolset generator sets `heartbeat_timeout = DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT`
> (10s → ~5s steady heartbeat) plus a **generous default `start_to_close_timeout` ceiling**
> that a dev can override when constructing the subagent toolset. The activity reads
> `heartbeat_timeout` from `activity.info()` and self-derives its interval, so the two stay
> consistent.

A **single** activity, `run_subagent_turn`, that sends the message to the child and streams
its reply to completion — one activity call per turn (cleaner than a send/consume split). It
uses **heartbeat state as an "already sent?" memo** so the common retry (a crash during the
long reply-stream) resumes *consuming* instead of re-sending. The activity takes the **child
subagent's `workflow_id`** as a plain arg (the parent workflow knows it from `start_<key>`)
plus the `(type, payload, expected_turn)` to send; it builds an `AgentClient(client,
child_workflow_id)` against the **child**, NOT the parent.

- **On entry, read `activity.info().heartbeat_details`:**
  - *not yet sent* → capture the child's stream head offset
    (`WorkflowStreamClient.create(client, child_id).get_offset()`), do the
    `send_agent_message` update, then `activity.heartbeat({sent: True, turn_id, turn_number,
    consumed_offset: head})`.
  - *already sent* (retry landed after the send) → **skip the send**; resume from the
    heartbeated `consumed_offset`.
- **Then stream:** subscribe to the child's stream from the offset, filter to `turn_id`,
  capture `AgentReply.output`, terminate on that turn's `turn_end` (surface an `error` event
  as failure — mirror `AgentClient.send_message`'s reduce loop, the exact template).
  **Heartbeat `{… consumed_offset: latest}` every N sec** (default ~5s); the dispatching tool
  sets a default **`heartbeat_timeout`** (~30s).
- **Return contract:** the **raw `output` dict** (+ `turn_id`/`turn_number`); the inline
  `send_<function>` tool re-validates against the handler's statically-known `output_type`
  (boundary validation). A turn that ends via `turn_end` with **no** preceding `AgentReply`
  (e.g. error-only) raises, so the tool surfaces an `is_error` result.
- **Best-effort, NOT idempotent (B still required).** The memo skips re-send on the common
  retry path, but a crash in the tiny window *between the update returning and the first
  heartbeat being durably recorded* would still re-send → double-submit. Closing that
  residual window is the deferred Workstream B, and the single activity **composes cleanly
  with it**: once the send step uses B's idempotent `submit_message`, a re-send just returns
  `DuplicateMessage` (same `turn_id`) and the activity continues — so the heartbeat memo
  degrades to a pure offset-resume optimization.
- **[RESOLVED — 2026-06-15] Activity → Temporal `Client` via a closed-over client on a
  class.** A `temporalio.client.Client` is needed inside the activity for subscribe **and**
  update against the *child* — `WorkflowStreamClient.from_within_activity()` does **NOT** work
  here (it infers the *running activity's own / parent* workflow and takes no `workflow_id`;
  the child stream needs `WorkflowStreamClient.create(client, child_id)`). We improve on the
  `tools.py` module-global precedent: the activity is a method of `SubagentActivities`, whose
  `__init__(client)` closes over the worker's client. No module-level mutable global; the
  client is an explicit construction dependency. The worker registers the bound method; a
  worker plugin will later instantiate the class from the worker's client (the plugin needs
  the client *instance*, which is why this can't live on the client itself).

## Workstream D — The subagent-toolset generator + example wiring
**Status:** 🚧 in progress (code ✅ — wiring + e2e test remain) · **Depends on:** A, C. **(This is
the end-to-end milestone — a real parent→subagent flow working. Idempotency, Workstream B, comes
*after* this.)**

> **Generator shipped (2026-06-17).** `harness/subagent_toolset.py` implements
> `subagent_toolset(agent_cls, *, key, task_queue, workflow_type=None)` (the originally-planned
> `label` arg was dropped — `key` names the subagent in every tool docstring, for consistency
> across the start/send/stop tools): it reads `agent_handlers(agent_cls)` statically and emits the namespaced
> `start_<key>` / `<key>_<fn>` (one per handler, signature typed to the child's real
> input/output models) / `stop_<key>` inline `tool_defn` callables, which resolve the live runner
> via the new private `_current_runner()` accessor. Re-exported as `agent.subagent_toolset`. Unit
> tests (tool names/namespacing, real-model signatures, docstrings, the no-handlers guard) in
> `harness/test_subagents.py`. **Remaining for D:** an example parent agent that wires a real
> subagent + an end-to-end test (see the demo section — now the Monty-subagent agent).

> **Runner-side wrapper shipped (2026-06-15).** The deterministic in-workflow half is done:
> `_SubagentInstance` (short `handle` + real `workflow_id` + `next_expected_turn` +
> `last_consumed_offset` + the FIFO ticket gate) nested in `_WorkflowStatus`, keyed by `handle`,
> with `has_subagent`/`register_subagent`/`subagent`/`remove_subagent`; and the runner methods
> `start_subagent` (start child + register, returns the short **`handle`** — not the
> `workflow_id`), `stop_subagent` (resolve handle → close signal + deregister), and
> `run_subagent_turn` (resolve handle → gate → exact `expected_turn`/`from_offset` →
> `execute_activity` against the real `workflow_id` → advance bookkeeping). Active subagents are
> surfaced on the `agent_status` query as `list[SubagentInfo]` (handle / agent_key /
> `workflow_id` / next turn) — the gate's ticket counters are an ordering implementation detail
> and are deliberately excluded. Unit tests for the gate/registry/handle indirection + the
> status projection in `harness/test_subagents.py`. **The toolset generator that builds on this
> is now also shipped** (see the "Generator shipped" note above); the only remaining D work is the
> example parent agent wiring + end-to-end test.

### Generator plan — LOCKED 2026-06-16 (verified feasible)

**Integration constraint (confirmed by reading the QA agent loop).** A parent exposes tools as
`tools=[function_param(fn) for fn in tool_functions]` and dispatches each `function_call` via
`runner.run_tool(call.id, callables_by_name[name], **args)`. `function_param` builds the model
schema from the callable's `__signature__`/`__annotations__`/`__doc__`/`__name__` (via Gemini's
`FunctionDeclaration.from_callable_with_api_option`); a `tool_defn` tool reaches the runner via
the ambient `_CURRENT_RUNNER` that `run_tool` parks. **So the generated toolset is just a
`list[tool_defn callables]` the parent folds into its tool set — inheriting native approval
gating + `tool_start`/`tool_end` lifecycle + the run_tool path for free.**

**Factory:** `subagent_toolset(agent_cls, *, key, task_queue, workflow_type=None)
-> list[Callable]`. Reads `agent_handlers(agent_cls)` **statically** (no workflow started); each
`_AcceptedHandler` already carries the real `input_type`/`output_type` classes. Emits, namespaced
by `key` (so a parent can wire several agent types without collisions):

- **`start_<key>() -> str`** — `tool_defn` calling `runner.start_subagent(key, workflow_type,
  task_queue)`; returns the short handle. Docstring names the subagent by `key`.
- **`<key>_<fn>(subagent: str, <param>: InputModel) -> OutputModel`** — one per handler. Its
  `__signature__`/`__annotations__` reference the child's REAL input/output pydantic models, so
  `function_param`/`from_callable` emits the correct **nested object schema** (field names +
  types + required) and the function is strongly typed end-to-end (verified). NOTE: Gemini's
  `from_callable_with_api_option` **drops nested per-field descriptions** — the handler's
  docstring still becomes the tool-level description, but carrying the input model's per-field
  `Field(description=...)` into the schema would need a description-aware `function_param`
  (possible follow-up; not required for a working, strongly-typed toolset). Body: validate
  `subagent` + coerce the arg dict → `InputModel`,
  `await runner.run_subagent_turn(subagent, "<fn>", payload)`, re-validate the returned dict →
  `OutputModel`, return it (boundary validation). Docstring = handler description + a note that
  `subagent` is the handle from `start_<key>`. **Nested model param** (Decision: chosen over
  flattening — faithful types + preserves Field descriptions).
- **`stop_<key>(subagent: str) -> str`** — `tool_defn` calling `runner.stop_subagent(subagent)`.

**Runner access:** add a small **private** `_current_runner()` accessor (parallel to `_current_tool_id()`) so
the closures resolve the live runner from `_CURRENT_RUNNER` (Decision #5 — no holder, no
`has_self`; that whole approach is abandoned).

**Result rendering:** the tool returns the typed `OutputModel`; the parent's dispatch loop
serializes BaseModel results with `model_dump_json()` so the model sees clean JSON, not a repr.

**Guardrail (preserve):** `tool_approval` is intentionally absent from `agent_interface`, so the
toolset has **no** approve-tool capability — a child's gated tools still escalate to a human and
are never auto-approved because a parent invoked the child.

### Demo — LOCKED 2026-06-17: the Monty-subagent conversational agent

The first real parent→subagent flow reuses the existing Monty pieces. The barebones
`MontyDynamicAgentWorkflow` (`agent/python/monty_dynamic_workflow/workflow.py`) — a no-model-in-
the-loop agent whose single `@agent.accepts` handler `run_script(RunScript) -> TextReply` executes
a script in the Monty sandbox — is the **ideal subagent**: it takes scripts and runs them. So a
new parent agent (a sibling of `conversational_workflow.py`'s `MontyChatAgent`, which is left
untouched) keeps the *same* conversational Gemini loop and script-writing system prompt, but
instead of running scripts inline via a `run_monty_script` `tool_defn`, it wires the script-runner
as a subagent: `subagent_toolset(MontyDynamicAgentWorkflow, key="monty", task_queue=TASK_QUEUE)`.
The model now `start_monty()`s an instance, sends scripts via `monty_run_script(subagent, RunScript)`,
and `stop_monty()`s it — a drop-in replacement for the inline tool. This exercises the whole design:
the handle indirection, multiple turns per subagent (gate + turn counter + offset resume), and the
`run_subagent_turn` activity against a real child.

- **Approval stance (LOCKED 2026-06-17):** the parent runs under `always_require_approvals` (as
  `MontyChatAgent` does), so it **gates the subagent tools** — every `start_monty` /
  `monty_run_script` / `stop_monty` call escalates to a human. The child keeps its own
  `dangerously_skip_all`, so the script's host calls run unguarded *inside the child* (a known
  difference from the inline agent, where host calls were gated in the parent — forwarding a
  gating policy into the child is a possible follow-up, not in this demo).
- **Dispatch loop:** generalized from the inline agent's single hardcoded tool to a
  `callables_by_name` map dispatched via `runner.run_tool(call.id, fn, **args)`, serializing the
  `BaseModel` tool results with `model_dump_json()` (per **Result rendering** above).
- **Worker:** registers the new parent + `MontyDynamicAgentWorkflow` + the Monty/host activities +
  **`SubagentActivities(client).run_subagent_turn`** (the activity that was previously registered
  nowhere). Runnable by hand with a `GEMINI_API_KEY`, like `MontyChatAgent`.
- **End-to-end test:** since the full parent path needs the live Gemini API (so it can't run in
  CI, exactly as `MontyChatAgent` has no automated test), the e2e assertion uses a tiny
  **model-free** test-only parent whose handler calls `runner.start_subagent` +
  `runner.run_subagent_turn` against a real `MontyDynamicAgentWorkflow` child under a
  `WorkflowEnvironment` — proving the activity + FIFO gate + offset-resume + registry against a
  real child without a model in the loop.

## Workstream E — Tests & docs
**Status:** 🚧 in progress — the unit tests landed with A/C/D; the parent→subagent integration
test is the open item (built alongside the demo above).

- Unit: handler discovery/validation + `agent_interface` shape + name-routed dispatch incl.
  two handlers sharing one input model (A — done); consume terminal/heartbeat logic (C);
  generated-tool schema shape (D).
- Integration (the near-term goal): **parent drives subagent end-to-end** (after D).
- Deferred with B: dedupe/`DuplicateMessage` validator + `submit_message` mapping (unit),
  and the "send-activity retry does **not** double-submit" integration assertion.
- Flip this doc's Status to ✅ and update each workstream as it lands.

---

## Dependency graph

```
A ✅ (handler-dispatch + agent_interface)
  └─> C ✅ (run_subagent_turn activity) ─> D 🚧 (toolset generator ✅ + runner wrapper ✅;
                                       │        REMAINING: example wiring + e2e test = MILESTONE)
                                       └─> B ⏸ (idempotency hardening — AFTER it works)
                                       └─> E 🚧 (unit tests ✅; integration test open)
```

Near-term path to a working parent/subagent: the mechanism (A, C, and D's generator + runner
wrapper) is shipped; what's left is **the example wiring + the end-to-end test** (the Monty-
subagent demo above). **B (idempotency) is intentionally last** — a production-reliability pass
run only after the flow is proven to work. (A is done; the rejection `details` B builds on
shipped with it.)
