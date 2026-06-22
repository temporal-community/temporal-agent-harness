# Unified Subagent Event Stream (client-side stream merge)

**Status:** ✅ **Implemented** (W1–W4 landed; see [Workstreams](#workstreams)). The merge layer
lives in [`harness/stream_merge/`](../temporal_agent_harness/harness/stream_merge) and is wired into
`AgentClient.send_message` / `attach`; the protocol deltas + the in-workflow `subagent_reply_received`
publish shipped. **One known caveat:** the merge can only read a subagent stream while that subagent
is LIVE — `workflow_streams` cannot yet subscribe to a *completed* (e.g. `stop_subagent`'d) workflow's
stream. This is **handled by graceful degradation, not a failure**: when the merge can't read a
stopped/completed child (most visibly on an `attach` after the child was stopped) it releases the
child's close gate so the parent renders fully and surfaces a non-fatal `subagent_stream_unavailable`
marker for the child's forgone detail (covered end-to-end by
`test_attach_after_stopped_subagent_degrades_gracefully`). An upstream `workflow_streams` fix that
makes a completed stream readable is in flight; until then the child's own turn detail is forgone on a
post-stop replay. This fulfills the
explicitly-deferred client work committed in [`agents-as-subagents.md`](agents-as-subagents.md)
**Decision #6** ("Collecting multiple agents' streams for a UI is a client-side concern:
`agent_client.py` will later learn to ad-hoc mount subagent workflow streams on demand so a UI can
assemble them … only the *consuming* of the child streams is deferred").

**Scope (planned):** a new `agent_client`-side merge layer (its own subdirectory — see
[Code layout](#code-layout)) consumed transparently by `AgentClient.send_message` / `attach`; small
protocol deltas in `harness/agent_protocol/` (`SubagentMessageSent.from_offset`, a new
`SubagentReplyReceived` event, `AgentMessageReply.accepted_offset`, and an `agent_id` stamped on
every `AgentEvent`); one in-workflow publish of the new event in `run_subagent_turn`; and an
in-workflow read of the `WorkflowStream` head via its existing private `_on_offset()` (no
`workflow_streams` change). The private `_stream_turn` is removed — `send_message` / `attach` both
go through the merge.

**Non-scope:** the independent per-agent streams themselves. **Each agent keeps exactly one
private stream and a subagent's stream is NEVER mirrored onto its parent's** (Decision #6,
[`agents-as-subagents.md`](agents-as-subagents.md)). This is desirable and intentional and is never
changed. This feature is purely a **client-side merge** of those independent streams into one
*logical* stream.

**Last updated:** 2026-06-21

---

## Goal

From the UI's perspective, an agent that drives subagents should look like **one logical event
stream** — the parent's events plus, recursively, every subagent's events — even though each agent
publishes to its own independent Temporal `WorkflowStream`. The UI must get this by calling the
exact same `AgentClient.send_message` / `attach` utilities it already uses; all the complexity of
observing `subagent_started`, pulling out the child `workflow_id`, mounting that child's stream
(recursively, for subagents-of-subagents, and for multiple concurrent subagents), and coalescing
everything into one ordered stream is hidden inside the client.

The hard part is **ordering on replay**. When a tab is refreshed mid-session, the client re-derives
the whole logical stream from backlog scattered across N independent streams that emitted
concurrently. The merged order must always be a **semantically possible** order under the event
protocol — never one that could not have happened in real time. We achieve this **without ever
trusting timestamps** (see [No timestamps](#no-timestamps)), by enforcing a small set of
**happens-before brackets** between a parent and each subagent turn.

---

## Mental model: the logical stream and its brackets

- **Logical stream.** The single ordered sequence the merge emits to the consumer: the root agent's
  stream interleaved with each mounted subagent's stream, recursively.
- **Stream isolation is preserved.** The merge only *reads* each agent's own stream; it never asks
  any agent to mirror another's events. The coalescing is entirely client-side.
- **Bracket.** Each subagent turn `T` on child `C` is *nested* inside a pair of markers on `C`'s
  **parent** stream:

  ```
  parent stream:   … subagent_message_sent(C, T) … subagent_reply_received(C, T) …
  child  stream:        ⌊ turn_started(T) … tool_start/end … reply(T) … turn_end(T) ⌋
                        └──────────── must appear strictly between the two markers ───┘
  ```

  The logical stream must place **all** of `C`'s turn-`T` events between `message_sent(C, T)` and
  `reply_received(C, T)`. Two causal edges make this mandatory, not cosmetic:

  - **Open edge.** `C` cannot begin turn `T` before the message that triggered it was sent. So
    `C`'s turn-`T` events *happen-after* `message_sent(C, T)`.
  - **Close edge.** The parent's `run_subagent_turn` activity physically blocks until it has
    consumed `C`'s turn-`T` through `turn_end(T)`; only then does the parent publish
    `reply_received(C, T)` and proceed. So everything the parent emits at or after
    `reply_received(C, T)` *happens-after* `C`'s entire turn `T`.

  A merge that violated either edge would show a semantically impossible history — e.g. a subagent
  reply landing before the subagent's own `turn_end`, or a subagent "speaking" before it was asked.

Everything else may interleave arbitrarily. Two *different* subagents driven concurrently, or any
events not related by a bracket, can appear in any relative order — we deliberately do **not**
maintain a global total order. Maintaining only the bracket edges is what keeps the algorithm
small.

---

## Locked decisions

1. **[LOCKED — 2026-06-20] Both brackets are gated.**
   *Open gate:* a mounted child stream's turn-`T` events are held until the merge has **emitted**
   `subagent_message_sent(C, T)`. *Close gate:* a `subagent_reply_received(C, T)` is held until the
   merge has **emitted** `C`'s `turn_end(T)`. Both keyed on `(child_workflow_id, child_turn_number)`.

2. **[LOCKED — 2026-06-20] No timestamps in the merge — live or replay.** <a name="no-timestamps"></a>
   Temporal runs a workflow and its activities (and different agents) on arbitrary, uncoordinated
   machines; an event's `timestamp` is best-effort wall-clock with no cross-machine ordering
   guarantee. It is display/debug metadata only and is **never** an input to the merge — not even as
   a replay tiebreak. Ordering comes entirely from (a) per-stream offset order and (b) the bracket
   gates.

3. **[LOCKED — 2026-06-20] Within-stream order is inviolable; only cross-stream order may differ.**
   Each cursor advances strictly by offset (the authoritative per-stream order). The *only* freedom
   the merge has — and the only thing that may differ between a live viewing and a later replay — is
   how it interleaves events *across* streams. This is an explicit, intended non-guarantee.

4. **[LOCKED — 2026-06-20] The merge always initializes at a `turn_started` (a quiescent point).**
   `attach` starts at offset 0 (the first `turn_started`). `send_message` starts at the
   server-returned acceptance offset and discards events until the target turn's `turn_started`.
   Because turns are sequential, a `turn_started` is provably a point with **zero open brackets**
   and an idle subagent subtree — so the merge never has to reconstruct in-flight bracket state.
   See [Quiescent start](#quiescent-start).

5. **[LOCKED — 2026-06-20] Acceptance offset is an *output* of submission, not a caller *input*.**
   The `send_agent_message` update handler returns the stream offset at acceptance; the client uses
   it as the read-start hint. Callers track no offsets. This eliminates the entire class of
   "caller supplied a bad offset and we only discovered it after the agent was already running" —
   there is nothing to validate after the side effect, because the offset is derived by the
   workflow itself. See [Acceptance offset](#acceptance-offset).

6. **[LOCKED — 2026-06-20] `subagent_reply_received` is published in-workflow, one event with an
   outcome.** Published by the in-workflow half of `run_subagent_turn`, right after the activity
   returns and before the FIFO gate is released — so it precedes the send-tool's `tool_end`. One
   event with an `ok | error` outcome (not two events); the close gate doesn't care which.
   Published on **every accepted** child turn (success or accepted-but-errored), and **never** on a
   pre-acceptance failure. See [The new event](#the-new-event) and [Edge cases](#edge-cases).

7. **[LOCKED — 2026-06-20] Correctness depends on the per-subagent FIFO ticket gate.** The merge
   assumes brackets *to a single subagent* never overlap. That is true **only** because the
   generated subagent toolset serializes a parent's sends to a given subagent through the FIFO
   ticket gate (Decision #2, [`agents-as-subagents.md`](agents-as-subagents.md)). Brackets across
   *different* subagents may freely overlap; the gates handle that because each is independent. This
   dependency is documented at both sites. See [Why FIFO matters](#why-fifo-matters).

8. **[LOCKED — 2026-06-20] One public path in; every event self-identifies its agent.** There is no
   separate single-turn reader on the public API — `send_message` and `attach` both go through the
   merge. A consumer that wants only one agent's events filters by **`agent_id`**, a field the
   harness stamps on **every** `AgentEvent` envelope (alongside `turn_id`/`turn_number`/`timestamp`)
   identifying the publishing agent. So the merged stream is always self-describing: each event says
   which agent it belongs to. The private `_stream_turn` primitive is removed; the subagent activity
   keeps a minimal internal single-child-stream reply-capture (it must not recurse — stream
   isolation). See [agent_id](#agent-id) and [API deltas](#api-surface-deltas).

---

## Protocol deltas

Three small additions. All are sandbox-safe pydantic/stdlib changes in `harness/agent_protocol/`,
plus one publish site and one offset accessor.

> A fourth, follow-on event — **`subagent_stream_unavailable`** — was added for graceful degradation. Unlike
> the others it is **never workflow-published**: the client-side merge synthesizes it when it gives up
> on an unreadable/stalled subagent stream. See [Graceful degradation](#graceful-degradation).

### `SubagentMessageSent` gains `from_offset` <a name="from-offset"></a>

`events.py` `SubagentMessageSent` today carries `{subagent_id, agent_key, workflow_id, function,
subagent_turn}` (the subagent-referencing field is named `subagent_id` — the short id of the child,
distinct from the envelope's `agent_id`, which is the publishing parent). Add:

```python
from_offset: int = Field(
    description="The offset in the SUBAGENT's own stream at which this turn's events begin — "
    "i.e. the child stream position the parent resumes consumption from for this turn. The "
    "merge positions the child cursor here the first time it mounts the child, so a resume "
    "that starts mid-session skips the child's pre-resume history (whose triggering "
    "message_sent events are not on the merged stream and could never open their gates)."
)
```

This value already exists at the publish site: `run_subagent_turn` passes
`from_offset = inst.last_consumed_offset` into the activity (`agent_workflow.py:1589`), and the
activity publishes `SubagentMessageSent` (Decision #6, [`agents-as-subagents.md`](agents-as-subagents.md)).
So publishing it costs nothing.

**Why it is load-bearing (not just an optimization).** On a `send_message` resume, the merge starts
at the target turn's `turn_started` with no children mounted. When it later emits
`message_sent(C, T)`, it must position `C`'s cursor at the offset where turn `T` begins. It must
**not** read `C` from 0: `C`'s turns `1…T-1` have `message_sent` markers that live *before* the
resume point and are therefore never emitted on the merged stream — so their open gates would never
open and those events would wedge the cursor forever. `from_offset` is exactly "where turn `T`
starts in `C`'s stream," so the merge skips the un-gateable history. (On `attach` from 0,
`from_offset` is 0 for turn 1 and naturally equals where the cursor already sits for later turns —
consistent.)

> Note the two offsets are unrelated address spaces: the handler's `accepted_offset` is a *parent*
> stream offset; `from_offset` is a *child* stream offset.

### New event: `subagent_reply_received` <a name="the-new-event"></a>

The symmetric counterpart to `subagent_message_sent`, closing the bracket on the parent stream.

```python
class SubagentReplyReceived(StreamEvent[Literal[AgentEventType.SUBAGENT_REPLY_RECEIVED]]):
    """This agent received a subagent's reply for one turn it dispatched — the close marker of
    the [message_sent … reply_received] bracket. Mirrors SubagentMessageSent's correlation fields."""
    type: Literal[...] = AgentEventType.SUBAGENT_REPLY_RECEIVED
    subagent_id: str   # the short id of the child (not the envelope's agent_id, which is the parent)
    agent_key: str
    workflow_id: str
    function: str
    subagent_turn: int   # the turn number ON THE CHILD (pairs with the matching message_sent)
    outcome: Literal["ok", "error"]   # accepted-but-errored turns still close the bracket
```

**Where it is published — in-workflow, deliberately.** In `run_subagent_turn`
(`agent_workflow.py:1537`), right after `execute_activity(...)` returns and **before**
`inst.release_gate()`. With an inline comment to the effect of:

```python
# Published in-workflow, NOT from the activity. The agent *is* the workflow: "received" must
# mean the AGENT (this workflow) has the reply in hand — not merely that the run_subagent_turn
# activity (which may run on a different machine) returned. Publishing here is also
# deterministic and needs no heartbeat dedup, unlike the activity-published message_sent marker
# (which must survive activity retries). Emitting it before release_gate() keeps it ahead of the
# send-tool's tool_end on this agent's stream.
```

The reply *payload* still rides the child's own `reply` event (merged in) and the send-tool's
`tool_end`; this marker is a thin correlation/close signal, intentionally not the reply body.

### `AgentMessageReply` gains `accepted_offset` <a name="acceptance-offset"></a>

`agent_protocol/agent_interface.py` `AgentMessageReply` today carries `{turn_number, turn_id,
pending}`. Add `accepted_offset: int`. `_handle_send_agent_message` (`agent_workflow.py:1128`)
captures the **current stream head at handler entry** and returns it.

```python
async def _handle_send_agent_message(self, message: AgentMessage) -> AgentMessageReply:
    accepted_offset = self._stream._on_offset()   # head BEFORE this handler publishes anything
    turn_id = str(workflow.uuid4())
    pending = self._status.has_pending_work
    turn_number = self._status.enqueue_message(message, turn_id)
    if pending:
        self._pub(turn_id, turn_number, MessageQueued(...))
    return AgentMessageReply(
        turn_number=turn_number, turn_id=turn_id, pending=pending,
        accepted_offset=accepted_offset,
    )
```

- **Idle agent:** `accepted_offset` is essentially "right before this turn's `turn_started`."
- **Queued message:** `accepted_offset` is the current head *mid the active prior turn* — exactly
  what the user described. The [skip-to-`turn_started`](#quiescent-start) preamble normalizes this.
- The value is a **read-start hint**; its only correctness requirement is
  `accepted_offset ≤ (this turn's turn_started offset)`. Capturing at handler entry guarantees it:
  the handler body is synchronous (no `await` that yields), so it runs atomically before the turn
  loop coroutine can publish `turn_started`, and offsets only grow. Imprecision beyond "≤" only
  costs a few extra discarded reads, never correctness.

> **Read the real head, not a publish counter.** Offsets are a single *global* log index
> (`workflow_streams/_stream.py:467` → `base_offset + len(log)`), and activity-published events
> (tool lifecycle, reply deltas, `subagent_message_sent`) enter that same log via signals. A naive
> workflow-side `_pub` counter would miss them and *undercount*. The head we read
> (`WorkflowStream._on_offset()`) reflects the actual log head. See [Feasibility](#feasibility).

### Every `AgentEvent` carries a short, tree-unique `agent_id` <a name="agent-id"></a>

Add `agent_id: str` to the `AgentEvent` envelope (`events.py`), stamped by the harness at publish
time exactly like `turn_id` / `turn_number` / `timestamp` — producers never set it. Its value is the
publishing agent's **short id** — a few `AGENT_ID_LENGTH`-wide (=6) hex segments joined by `-`, *not*
the full `workflow_id` — which is cheap for a model/UI to reproduce and compact on the wire.

**The id is TREE-UNIQUE.** A top-level agent's id is a single segment; a subagent's id is its
parent's id plus one fresh segment (`<parent>-<6hex>`), so ids deepen with the subagent tree
(`a1b2c3` → `a1b2c3-d4e5f6` → …). Each agent rerolls its own children's fresh segments for
in-registry uniqueness, and prefixing with the (already tree-unique) parent id extends that across
the whole tree — so **no two agents in one merged stream ever share an `agent_id`**. That is what
makes "filter/group the merged stream by `agent_id`" unambiguous (the envelope carries no
`workflow_id`, so `agent_id` is the only per-event identity a consumer can group a child's own events
by). `AgentId` (`agent_interface.py`) is the pydantic-constrained shape — one-or-more hex segments
joined by `-` — enforced when an `AgentConfig` crosses the data converter into the workflow (which is
also why `AgentConfig` is a pydantic model, not a plain dataclass).

**Where an agent's id comes from.** Each agent resolves its own id once in
`AgentWorkflowRunner.__init__`: `config.agent_id` if the caller set it, else a generated
single-segment `workflow.uuid4().hex[:AGENT_ID_LENGTH]`. It is surfaced on the `agent_status` query
(`AgentStatus.agent_id`) so a consumer can map a session's events to the agent.

**A parent assigns its subagents' ids.** `start_subagent` mints the `handle` it references each child
by (`_fresh_subagent_handle` = this agent's own id plus a fresh, reroll-deduped segment) and pushes
it down as the child's `AgentConfig.agent_id` (via `AgentConfig.model_copy`) — so the child stamps
*the same* id on its own events. This unifies "the id the parent references the subagent by" (on the
parent's `subagent_started` / `subagent_message_sent` / `subagent_reply_received`) with "the id on the
subagent's own stream" — the merge/UI correlates them directly, with no `workflow_id`→handle mapping.
The reroll is **load-bearing** for tree-uniqueness (see `_fresh_subagent_handle`).

**Stamping it, workflow- and activity-side.**
- Workflow-side publishes (`_pub`) stamp the runner's resolved `self._agent_id`.
- Activity-side publishes (`TurnEventPublisher.publish`) stamp `TurnStreamContext.agent_id`. The short
  id is *not* derivable from `activity.info()` (which only knows the `workflow_id`), so — unlike the
  earlier workflow_id approach — it must be threaded: `TurnStreamContext` gains an `agent_id` field,
  set wherever the runner builds the context (`current_stream_context`) and carried into every
  publishing activity (tool, model, `run_subagent_turn`). One small field, set in one place.

**The merge is `agent_id`-agnostic.** Mounting + gating key on `workflow_id` (to subscribe) and turn
numbers — never on `agent_id` — so the short-id switch needs no merge change. `agent_id` is purely
for the *consumer*: filter the merged stream to one agent, or attribute a child's events to the
subagent shown in the parent's status (both share the handle). The client's per-turn error-surfacing
identifies the root turn by its globally-unique `turn_id`, not by `agent_id`.

---

## The merge algorithm

A single **gated k-way merge** over per-stream cursors, parameterized by one `select(...)` policy
that differs only between live and replay.

### State

```python
opened:           set[tuple[str, int]] = set()  # (child_wf_id, turn): message_sent emitted ⇒ open gate satisfied
child_turn_ended: set[tuple[str, int]] = set()  # (child_wf_id, turn): child turn_end emitted ⇒ close gate satisfied
cursors:          dict[str, Cursor]    = {}      # workflow_id -> live cursor
mount_seq = 0                                    # monotonic mount counter → deterministic replay tiebreak

class Cursor:
    workflow_id: str
    is_child: bool            # False only for the root cursor
    mount_index: int          # assigned at mount; used by the replay select policy
    sub: Iterator             # WorkflowStreamClient.create(client, workflow_id).subscribe(from_offset=…)
    head: AgentEvent | None   # the peeked next event (offset-ordered), or None if not yet fetched / idle
    skip_until_turn_id: str | None = None   # root-only preamble (send_message); None ⇒ no skip
```

### Gates — when a head may be emitted

```python
def ready(cur: Cursor, ev: AgentEvent) -> bool:
    # OPEN gate: every event on a mounted child stream waits for its triggering message_sent.
    if cur.is_child and (cur.workflow_id, ev.turn_number) not in opened:
        return False
    # CLOSE gate: a reply_received waits for the referenced child turn to have ended.
    if ev.event.type == SUBAGENT_REPLY_RECEIVED:
        if (ev.event.workflow_id, ev.event.subagent_turn) not in child_turn_ended:
            return False
    return True
```

Both gates compose, which is what makes recursion work: an *intermediate* agent's stream is itself
a mounted child (open-gated by its own parent's `message_sent`) **and** emits its own
`reply_received` for its grandchildren (close-gated on the grandchild's `turn_end`). A single event
may be subject to both gates.

### Emitting a head updates the gate-enabling sets

```python
def on_emit(cur: Cursor, ev: AgentEvent) -> MountChild | UnmountChild | None:
    et = ev.event.type
    if et == SUBAGENT_MESSAGE_SENT:
        opened.add((ev.event.workflow_id, ev.event.subagent_turn))   # open the child's turn gate
        return MountChild(ev.event.workflow_id, ev.event.from_offset) # engine mounts (lazy, idempotent)
    if et == SUBAGENT_STOPPED:
        return UnmountChild(ev.event.workflow_id)                     # engine closes + drops the cursor
    if et == TURN_END and cur.is_child:
        child_turn_ended.add((cur.workflow_id, ev.turn_number))       # satisfy parent's reply_received
    return None
```

**Mounting is driven solely by `message_sent`** — it is the one signal that carries both the child
`workflow_id` (where to mount) and `from_offset` (where to position), and it is present on both
entry paths. `subagent_started` is emitted as an ordinary lifecycle event for the UI to render but
does **not** drive mounting (on `attach` from 0 the child is idle between its `subagent_started` and
first `message_sent`, so deferring the mount to `message_sent` loses nothing; on a `send_message`
resume the historical `subagent_started` isn't on the stream at all).

**Unmounting is driven by `subagent_stopped`** (and by a child cursor turning out
unreadable/exhausted — see [Graceful degradation](#graceful-degradation)). `on_emit` returns an
`UnmountChild` and the engine closes that cursor and drops it from the active set. Because a stop
occurs at a quiescent point — by then every one of that child's turns is drained (its last
`reply_received` required the child's `turn_end`) and a stopped subagent's id never reappears — no
gated event is ever stranded. Unmounting is not just tidiness: each open cursor holds one long-poll
*update* in flight against its workflow, and Temporal caps concurrent in-flight updates **per
workflow at 10**, so releasing a stopped child's cursor frees its slot. (A `send_message` terminal
tears down all child cursors wholesale.)

### The loop

```python
async def run_merge(root_wf_id, *, root_from_offset, skip_until_turn_id, select):
    mount(root_wf_id, from_offset=root_from_offset, is_child=False,
          skip_until_turn_id=skip_until_turn_id)
    while not done():
        await ensure_heads()                         # peek each cursor's next event (see preamble note)
        candidates = [c for c in cursors.values()
                      if c.head is not None and ready(c, c.head)]
        if not candidates:
            await wait_for_progress()                # live: await the next event on any cursor
            continue
        cur = select(candidates)                     # see select policies below
        ev = cur.head
        emit_to_consumer(ev)
        on_emit(cur, ev)
        cur.head = None                              # force the next peek for this cursor
```

**Root preamble (the skip).** For `send_message`, the root cursor reads from `accepted_offset` and,
in `ensure_heads`, **discards events without emitting them and without calling `on_emit`** until it
reaches `turn_started(skip_until_turn_id)`; from then on it behaves normally. Discarding (rather
than processing) the prior turn's tail is what keeps merge state empty at the quiescent start: any
prior-turn `message_sent`/child activity in that tail is simply never tracked, and since turns are
sequential the prior turn has fully ended (all its brackets closed) by our `turn_started`. For
`attach`, `skip_until_turn_id` is `None` and the first event at offset 0 is already a `turn_started`
— the preamble is a no-op.

### `select` policies — the only live/replay difference

- **Live (tailing the heads):** emit each ready head as soon as it arrives (arrival order). No
  global watermark — an idle child must never stall the whole logical stream, and a gated head
  correctly *waits* for its enabling event (that wait is the gate doing its job; the parent really
  was blocked). Realized event-driven: `wait_for_progress` awaits the next event on any cursor and
  re-checks gates.
- **Replay (attach from backlog):** every cursor's backlog is available, so `select` is a
  **deterministic structural** choice — pick the candidate with the lowest `mount_index` (root
  first, then children in mount order). Reproducible across replays; uses no clock.

Because cross-stream interleaving may differ between these two policies, a live viewing and a
post-refresh replay can interleave concurrent events differently — but both always respect
within-stream order and both brackets, so both are semantically valid (Decision #3).

### Termination

- **`send_message`:** terminate when the target turn's `turn_end` is emitted. By the close gate,
  the parent's `reply_received(C, T)` (and hence the parent's own `turn_end`) cannot be emitted
  until every triggered child `turn_end` has been emitted — so at the target `turn_end` the entire
  subtree for that turn is already fully drained. Tear down all child cursors.
- **`attach`:** mirror today's terminal (`agent_client.py:384`) — on each root `turn_end`, re-query
  status and stop once `turn_active` is false and all turns through `current_turn` have ended. At
  that quiescent point all brackets are closed, so no child cursor holds gated events.

### Quiescent start (`send_message`) <a name="quiescent-start"></a>

`send_message` resumes at a **quiescent point** — the submitted turn's `turn_started`, reached by
skipping (`skip_until_turn_id`). The theorem that makes that clean: **a parent `turn_end(N)` implies
the entire subagent subtree is idle.** The parent's turn handler cannot return (cannot publish
`turn_end(N)`) while still `await`ing a `run_subagent_turn`; that activity returns only after the
child's `turn_end`; recursively every descendant turn triggered by turn `N` has ended, and idle
subagents emit nothing until messaged again. So a turn boundary has **empty bracket state** — the
merge starts there with nothing to reconstruct, and a scalar offset fully describes it.

### Non-aggressive resume (`attach` from any offset) <a name="non-aggressive-resume"></a>

`attach` resume does **not** require a quiescent point — it starts the root at the given offset with
**no skip**, and that's still safe without any bracket reconstruction. The key: the merge only ever
*mounts* a child when it *emits* that child's `subagent_message_sent`. So if the resume offset falls
*inside* a subagent `C`'s turn (after `C`'s `message_sent`, before its `subagent_reply_received`):

- `C` is **never mounted** (we don't replay its `message_sent`), so `C`'s own turn events are simply
  **absent** from the resumed stream — the invariant the consumer relies on. (A UI that already
  rendered `C`'s detail before disconnecting loses nothing. Note this cuts both ways: a consumer that
  disconnected *partway* through `C`'s turn does **not** get the unseen remainder on resume either —
  resume is lossless only at root-event granularity; see below — so a consumer that needs every
  subagent event across a mid-turn reconnect attaches from 0.)
- `C`'s `subagent_reply_received` would otherwise close-gate forever (its `turn_end` never emitted),
  so the engine's **unmounted-stuck give-up** releases it at once: a buffered `reply_received` for a
  child that isn't mounted can never be satisfied, so the merge gives up immediately (no marker — we
  never showed `C`'s detail, so nothing is "lost"), and the parent's reply + everything after it flow.
- Subagents dispatched **at/after** the offset see their `message_sent` emitted normally → mount →
  bracket-merge with full detail.

This is strictly more complete than fast-forwarding to the next turn (the parent's in-flight turn tail
is preserved). The resume offset the merge hands back (via `on_item`'s second arg) is a **root-stream**
offset that advances **only on root events** — every subagent event between two root events carries the
same value. So resume granularity is **per-root-event, not per-subagent-event**: any root offset is a
*valid* resume point (never a broken ordering, never a wedge), but resume is *lossless* only at root
granularity — a consumer that disconnects mid a subagent turn forgoes that subagent's remaining detail
on resume (and gets no marker — the merge treats it as already delivered). A *re-dispatch* of a
given-up child later in the stream re-mounts it and clears it from `gone`, so its new turn's close gate
holds again.

### Deadlock freedom

- A parent held at `reply_received(C, T)` waits for `C.turn_end(T)`. `C` is **not** blocked: its
  open gate `message_sent(C, T)` is emitted *earlier* on the parent stream (it precedes
  `reply_received(C, T)` by construction), so `C` drains turn `T` and reaches `turn_end(T)`,
  releasing the parent.
- A child held before turn `T` waits for `message_sent(C, T)`. The parent is **not** blocked before
  emitting it (its only block, `reply_received(C, T)`, comes later), so it reaches and emits
  `message_sent(C, T)`, releasing the child.
- Distinct subagents have disjoint bracket pairs; recursion nests brackets, so a parent's
  reply-gate transitively waits on a grandchild — composing without cycles.
- Same-subagent concurrency is serialized by the FIFO ticket gate ([below](#why-fifo-matters)):
  `reply_received(C, 1)` is emitted before the gate releases call 2, so `message_sent(C, 2)` cannot
  appear until after `reply_received(C, 1)` — brackets to one subagent never overlap.

### Graceful degradation <a name="graceful-degradation"></a>

A subagent stream can fail to deliver for reasons short of normal end-of-stream — most importantly a
**stopped/completed child** whose stream isn't yet replayable (the known `workflow_streams`
limitation), or the **per-workflow concurrent-in-flight-update cap** (`subscribe()` long-polls via an
`__temporal_workflow_stream_poll` *update*, and Temporal caps concurrent in-flight updates **per
workflow at 10**; the 11th poll fails with `RPCError: limit on number of concurrent in-flight
updates has been reached (10)`). The failure can surface three ways: the pull **raises** (the cap),
the pull **cleanly ends** mid-turn (`StopAsyncIteration` from a completed workflow whose backlog
stops before `turn_end`), or — the one we actually saw — the pull **hangs** (a poll against a
completed child that neither yields nor returns).

The governing principle (**no retries** — recovery is a fresh `attach`, i.e. a page refresh): **a
subagent stream that can't deliver must never crash *or wedge* the parent.** The parent's own stream
is self-sufficient — it already carries the child's `subagent_reply_received` and the send-tool's
result — so the parent renders fully; only the child's own turn *detail* is forgone. Concretely, when
the merge gives up on a child it:

1. **Releases the child's close gates.** The load-bearing fix: a child marked `gone` can never
   deliver a `turn_end`, so a `subagent_reply_received` for it is treated as satisfied
   ([Gates](#gates) `gone` set). Without this, a dead child's `turn_end` that never comes leaves the
   parent's `reply_received` — and, by within-stream order, *everything sequenced after it* (the
   parent's `reply`, `turn_end`, even other concurrent subagents' later markers) — stranded behind a
   gate that never opens. That stranding was the observed hang.
2. **Drops the child cursor**, closing its subscription (freeing its in-flight poll slot).
3. **Emits a non-fatal `subagent_stream_unavailable` marker** (only when a turn was actually abandoned — its
   `subagent_message_sent` was emitted but its `turn_end` never was), stamped with `agent_id ==
   subagent_id` so a UI that groups by `agent_id` routes it to that subagent's view (and a root-only
   consumer filters it out). It is the signal that hands recovery to the consumer: "this subagent's
   detail is missing — refresh to retry."

**How a give-up is triggered** covers all three failure shapes: a **raised** read error or a
**clean mid-turn end** is reaped in `_collect_completed_pulls` → give up; a **hang** is caught by a
liveness backstop — when a buffered parent `reply_received` is close-gated on a child, that child gets
a **per-child deadline** (`stall_grace_seconds`, default 5s, configurable per call via
`AgentClient.send_message` / `attach`'s `subagent_stall_grace_seconds`). The deadline counts from the
child's **own** last delivery — re-armed each time *it* delivers, untouched when a *sibling* delivers
— so a child is given up only when **it itself** is silent for the whole window while blocking. (A
single shared timer would be re-armed by any cursor's delivery, so a chatty sibling could defer a dead
child's give-up indefinitely; the per-child deadline is what bounds it.) This bound is a **liveness**
mechanism, *not* an ordering input (ordering stays gate/offset-driven and clock-free); it applies
*only* while a parent reply is close-gated on a child, so an idle/slow LIVE stream that isn't
close-gate-blocked is waited on indefinitely and never spuriously given up. In replay a buffered
`reply_received` proves the child turn already completed at record time, so a *readable* child delivers
well within the window — only a dead/unreachable one trips it. (Once the upstream
readable-completed-stream fix lands, a stopped child becomes readable and delivers its `turn_end`
normally, so the backstop simply stops firing.)

A failed **root** (no other stream the consumer is here for) ends the merged stream cleanly rather
than raising out of the generator (which would 500 the BFF and tear the SSE response down mid-frame).

> **Why the cap is reached at all (an upstream constraint, not a merge bug).** `_on_poll`
> (`workflow_streams/_stream.py:394`) blocks in `workflow.wait_condition(...)` until new items
> arrive or the stream detaches. A client that disconnects mid-poll **cannot reclaim** that parked
> server-side update — Temporal updates are not client-cancellable — so a subscribe to an *idle*
> child (one between turns, not yet stopped) leaves a slot held until that child's next event.
> Re-attaching an idle session on a fixed tick therefore *accumulates* parked polls on a live
> subagent until the cap is hit, which also starves the parent's own `run_subagent_turn` dispatch
> (an `execute_update` on the same child) — the "agent goes unusable" symptom. The merge degrades
> gracefully when this happens, **and unmounts stopped children** to free slots, but it cannot
> un-park a server-side poll. The real mitigation is on the **consumer**: don't re-attach an idle
> session repeatedly — the example UIs poll the cheap `agent_status` *query* (queries don't consume
> update slots) while idle and only open a fresh `attach` once status shows genuinely new work. An
> upstream `workflow_streams` cancel-on-detach fix is in flight.

---

## Why the FIFO ticket gate matters <a name="why-fifo-matters"></a>

The merge assumes that for a single subagent `C`, the brackets `[message_sent(C, T) …
reply_received(C, T)]` are **non-overlapping** (turn `T+1`'s open marker never appears before turn
`T`'s close marker). That assumption is guaranteed *only* by the per-subagent FIFO ticket gate in
`run_subagent_turn` (Decision #2, [`agents-as-subagents.md`](agents-as-subagents.md);
`agent_workflow.py:745` `_SubagentInstance` + `run_subagent_turn`): a parent's `asyncio.gather`-ed
sends to the same subagent take tickets synchronously in call order and run one at a time, and
`reply_received` is published before `release_gate()`. Across *different* subagents brackets may
overlap freely — the gates are independent and handle that. **This coupling must be noted at both
sites** (the toolset/`run_subagent_turn` code and this doc) so a future change to the gate doesn't
silently break the merge.

---

## Feasibility (verified against the vendored `workflow_streams`) <a name="feasibility"></a>

**Offsets are a single global log index, and the workflow can name its current head.**
`WorkflowStream` keeps one append-only `_log`; the head is `base_offset + len(_log)`
(`_stream.py:467`, exposed externally via the `__temporal_workflow_stream_offset` query that the
client's `get_offset()` calls — `_client.py:624`). The per-item `offset` on each stream item (the
merge's cursor records it as `head_offset`) is an index into that same global log, so the producer
head and a stream's per-item offsets share one address space. **No `workflow_streams` change needed:**
the runner just **retains the `WorkflowStream`** (today it keeps only the topic handle —
`self._events`, `agent_workflow.py:1082`) and reads its head via the existing private `_on_offset()`
(`_stream.py:467`). Depending on that private accessor is fine — this harness is Temporal's own code,
not third-party, so "experimental/private" is no reason to avoid it. Reading the head inside the
update handler is deterministic workflow state access.

**Why a publish counter won't do.** Activity-published events reach the log via signals
(`publisher_from_activity` → `WorkflowStreamClient.from_within_activity`), so they advance the
global offset without going through the workflow's `_pub`. The acceptance offset must therefore be
the real log head, which `_on_offset()` returns, and which already reflects every signal the
workflow has processed at handler time.

**De-risked even if the API surprises us.** Because `accepted_offset` is only a read-start hint and
the merge skips to `turn_started`, a too-*early* value just discards a few extra events; only a
too-*late* value could miss the turn start, and capturing at handler entry makes that impossible
(`accepted_offset ≤ turn_started` always). The one open item to confirm in implementation:
`WorkflowStream` log **truncation** (`_stream.py:335`) — if a long session truncates early history,
`attach` from 0 can't replay below `base_offset`. The harness doesn't call `truncate` today; if it
ever does, document that `attach` replays from the live base, not absolute 0. Out of scope here.

---

## Edge cases <a name="edge-cases"></a>

1. **Accepted-but-errored child turn.** The child still emits `turn_end(T)` (the runner's `finally`,
   per the "always emit `turn_end`" resolution in [`agents-as-subagents.md`](agents-as-subagents.md)),
   so the close gate can satisfy — **but** the parent must still publish
   `subagent_reply_received(C, T, outcome="error")`, or the bracket never closes and the parent's
   tail wedges. This aligns with the existing turn-counter logic that advances on
   `SubagentTurnError`/`SubagentNoReply`. The close marker uses `T` = the child's **actual accepted
   turn number**, which the activity threads through the `ApplicationError` details and the parent
   reads via `_accepted_turn_from_error` — the *same* number the opening `subagent_message_sent`
   carried — so the close-gate key `(workflow_id, subagent_turn)` matches the open marker by
   construction (not by re-deriving `expected`).
2. **Pre-acceptance failure (`StaleTurn`/`AgentBusy`).** No child turn exists; no `turn_end` will
   ever come. We must **not** publish `reply_received` here (it would gate forever on a nonexistent
   `turn_end`). This matches the counter logic that advances nothing on a pre-acceptance rejection.
3. **`stop_subagent` mid-session / parent `TERMINATE`.** A normal stop happens at a quiescent point,
   so no bracket is open — the merge unmounts that child on its `subagent_stopped` (closing the
   cursor, freeing its poll slot — see [Mounting/unmounting](#emitting-a-head-updates-the-gate-enabling-sets)).
   For abnormal termination the teardown still ends the logical stream cleanly even if a child
   cursor is mid-turn (force-close every cursor on merge exit; the consumer already sees the
   terminal on the root). A child cursor that becomes unreadable mid-stream is likewise dropped, not
   fatal (see [Graceful degradation](#graceful-degradation)).
4. **Dead/unreadable child on replay (the observed hang).** On replaying a session whose subagent
   was stopped, the parent's `subagent_reply_received(C,T)` is already in the backlog, but the child
   `C` is now a completed workflow whose stream can't be read — so mounting it on
   `subagent_message_sent` yields a pull that hangs (or errors, or cleanly ends mid-turn). The
   close gate would then strand `reply_received` and, by within-stream order, the parent's entire
   tail. **Handled** by [Graceful degradation](#graceful-degradation): the merge gives up on the
   child (releasing its close gate, dropping its cursor, emitting a `subagent_stream_unavailable` marker),
   so the parent renders fully. The hang specifically is broken by the `stall_grace_seconds`
   liveness backstop. No retry — a refresh re-attempts. (The live equivalent — a child that dies
   mid-turn — also resolves here once the parent's activity errors and publishes
   `subagent_reply_received(outcome="error")`, which the same give-up path releases.)
5. **Gate-set growth.** `opened` / `child_turn_ended` / `gone` grow over a long `attach` session
   (cheap `(str, int)` tuples / `str`s). Bounding to recent turns is a possible future
   optimization, not required.

---

## API surface deltas

The public surface gets *smaller* while the semantics get *stronger*:

- `AgentClient.send_message(msg_type, payload, expected_turn, *, on_item, timeout,
  subagent_stall_grace_seconds)` — **`from_offset` removed.** The client reads `accepted_offset` from
  the submit reply internally and drives the merge from there; callers track no offsets. Phase 1
  (`_submit_message`) still runs eagerly so `StaleTurnError`/`AgentBusyError` raise *before* any
  streaming — and, critically, before the merge is even constructed, so there is no failure path after
  the agent has accepted. `subagent_stall_grace_seconds` (default 5s) tunes the liveness backstop (see
  [Graceful degradation](#graceful-degradation)).
- `AgentClient.attach(*, on_item, from_offset=0, subagent_stall_grace_seconds)` — **`from_offset` is an
  arbitrary resume offset.** `0` (default) is the blank-slate full replay (a freshly-loaded tab). Any
  value the *previous* stream handed back resumes from exactly there — **no skip** — so a long-lived
  debugging/states UI catches up incrementally instead of re-replaying on every reconnect. Resuming is
  safe at *any* offset (not just turn boundaries): the merge only mounts a child when it emits that
  child's `subagent_message_sent`, so a subagent whose turn began before `from_offset` is never
  mounted — its events are absent and its later `subagent_reply_received` is released by the merge's
  unmounted-stuck give-up — while subagents dispatched at/after it merge normally (this is exactly the
  [non-aggressive resume invariant](#non-aggressive-resume)). The resume offset reaches the consumer
  via `on_item(item, resume_offset)`'s second arg — a stable **root-stream** resume cursor that
  advances **only on root events**, explicitly *not* the merged display ordinal (the cross-stream
  interleaving is not a
  resumable position — Decision #3). Any root offset is a *valid* resume point, but resume is *lossless*
  only at root granularity: a reconnect mid a subagent turn forgoes that subagent's remaining detail
  (a fresh `from_offset=0` attach gets it all). `send_message` passes it through but doesn't resume on
  it (the chat path reattaches via `attach`).
- `AgentMessageReply` gains `accepted_offset: int` (internal plumbing; the BFF/UI never sees or
  stores it).
- The private `_stream_turn` (`agent_client.py:222`) is **removed** — there is one public path (the
  merge) for both `send_message` and `attach`. The subagent activity (`run_subagent_turn`) keeps a
  **minimal internal single-child-stream reply-capture** loop (capture `turn_id`'s reply, stop at
  `turn_id`'s `turn_end`): it deliberately reads ONLY the child's own stream — no recursion into
  grandchildren and no bracket gates — because, per stream isolation, it must not consume (and would
  wedge a close gate on) a grandchild turn it never mounts.

---

## Code layout <a name="code-layout"></a>

The algorithm is intricate enough to warrant its own well-composed subdirectory with explicit
docs, kept as minimal as a single human can hold in their head (per the project's guidance for
this feature). Indicative shape (names to be finalized in implementation):

```
harness/stream_merge/
├── __init__.py        # public entry the AgentClient calls; re-exports the merge driver
├── README.md          # this design distilled to an implementer's reference
├── cursor.py          # Cursor + per-stream subscribe/peek/advance, the root preamble skip
├── gates.py           # opened / child_turn_ended sets + ready() + on_emit() (pure, unit-testable)
└── merge.py           # run_merge loop + the live / replay select policies + termination
```

`gates.py` is pure and deterministic — it is where the bracket invariants live and where the bulk
of the unit tests point (feed it scripted multi-stream event sequences with adversarial
interleavings and assert the emitted order always respects within-stream order + both brackets).

---

## Open questions / future

- **Multi-message / queuing UX.** The design *handles* a queued `send_message` (the
  skip-to-`turn_started` reaches the queued turn once it starts), but the product question of
  whether a UI watching live should also use the per-message merged stream vs. one long-lived
  `attach` is left to the consumer.
- **Gate-set bounding** for very long `attach` sessions (Edge case 5).
- **Truncation interaction** with `attach` from 0 ([Feasibility](#feasibility)).

---

## Workstreams

Status legend: 🧭 not started · 🚧 in progress · ✅ done.

- **W1 — Protocol deltas** ✅ — `SubagentMessageSent.from_offset`, `SubagentReplyReceived` (+ enum
  member + union + `agent_protocol` re-export), `AgentMessageReply.accepted_offset`, and a short
  **`AgentEvent.agent_id`** (see [agent_id](#agent-id)): resolved per agent in the runner
  (`AgentConfig.agent_id` — `AgentId`-constrained on the now-pydantic `AgentConfig` — else a
  generated single segment), stamped in `_pub` and in `TurnEventPublisher.publish` via the new
  `TurnStreamContext.agent_id`, surfaced on `AgentStatus.agent_id`, and pushed down by
  `start_subagent` as each child's tree-unique id (== its handle = parent id + a fresh segment).
  `subagent_reply_received` published in-workflow by `run_subagent_turn` (success +
  accepted-but-errored, never pre-acceptance); `from_offset` populated on the existing
  `subagent_message_sent` activity publish.
- **W2 — Runner offset wiring** ✅ — runner retains the `WorkflowStream`; `_handle_send_agent_message`
  returns `accepted_offset` read from the existing private `_on_offset()` (no `workflow_streams`
  change).
- **W3 — The merge layer (`stream_merge/`)** ✅ — `gates.py` (pure) + `cursor.py` (peek-ahead +
  skip preamble) + `merge.py` (gated k-way merge loop + `select_live`/`select_replay`) +
  `__init__.py` + `README.md`. Wired into `AgentClient.send_message` (`_merged_turn`, live policy)
  and `attach` (`_merged_attach`, replay policy); `_stream_turn` removed and the subagent activity
  given its own minimal single-child reader (`_consume_child_turn`). BFF (`app.py`) updated to the
  offset-free API.
- **W4 — Tests** ✅ — `tests/harness/test_stream_merge.py`: pure gate tests + engine tests over
  scripted in-memory streams via a fake client, with a validity checker asserting within-stream
  order + both brackets on every ordering, plus replay determinism, reused-child, concurrent
  subagents, grandchild recursion, the resume/skip-preamble cases, and the per-child stall-deadline
  backstop (a dead child is given up on its own deadline despite a chatty sibling). Integration in
  `tests/examples/monty/test_subagent_e2e.py`: a real parent→subagent merged `send_message` against a
  **live** subagent, plus `test_attach_after_stopped_subagent_degrades_gracefully` — a real
  attach-after-`stop_subagent` that exercises the actual degradation path (the completed child is
  unreadable, so the merge releases the close gate, the parent renders fully, and a
  `subagent_stream_unavailable` marker is surfaced). An upstream `workflow_streams` terminated-stream
  fix would make that child readable and the degradation moot.

```
W1 ─┐
    ├─> W3 (merge layer) ─> W4 (tests)
W2 ─┘
```
