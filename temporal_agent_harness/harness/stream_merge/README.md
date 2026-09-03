# `stream_merge` — the client-side unified subagent event stream

This package merges a root agent's event stream with, recursively, every subagent stream it drives
into **one logical stream** — while each agent keeps its own independent Temporal `WorkflowStream`
(stream isolation is never violated; the merge only *reads* each stream). It is what lets
`AgentClient.send_message` / `attach` show a UI a parent + all its subagents as a single ordered
stream without the caller knowing subagents exist.

Full design + rationale: [`docs/internal/unified-subagent-event-stream.md`](../../../docs/internal/unified-subagent-event-stream.md).

## The one invariant

Ordering **never** uses timestamps (Temporal clocks across machines are uncoordinated). The merged
order is always a *semantically possible* order, guaranteed by two happens-before **brackets** that
nest a subagent turn `T` on child `C` inside markers on `C`'s parent stream:

```
parent:  … subagent_message_sent(C,T) … subagent_reply_received(C,T) …
child:        ⌊ turn_started(T) … reply(T) … turn_end(T) ⌋
```

- **Open gate** — a child stream's turn-`T` events wait until the parent's `subagent_message_sent(C,T)`
  has been emitted (a child can't speak before it was asked).
- **Close gate** — a `subagent_reply_received(C,T)` waits until `C`'s `turn_end(T)` has been emitted
  (the parent's `run_subagent_turn` really blocked on the child's whole turn).

Everything not related by a bracket may interleave in any order. Within a single stream, offset
order is inviolable; only *cross-stream* interleaving may differ between a live view and a replay.

Correctness depends on the per-subagent **FIFO ticket gate** in `run_subagent_turn`: it serializes a
parent's turns to one subagent, so brackets to a given subagent never overlap. Don't break that
without revisiting this.

## Files

| file | role |
| --- | --- |
| `gates.py` | **Pure.** The two sets + `ready()` / `on_emit()`. No I/O, no asyncio — the bracket invariants live here and are unit-tested against adversarial scripted interleavings. |
| `cursor.py` | One stream's peek-ahead read position: at most one buffered `head`, one in-flight `pull`, and the root-only "skip to my `turn_started`" preamble. |
| `merge.py` | The gated k-way merge loop + the `select_live` / `select_replay` policies (the sole live/replay difference) + `merge_stream` entry. |

## How it runs (merge.py `_drive`)

1. Ensure every cursor without a head and not exhausted has a `pull` task in flight.
2. Candidates = cursors whose buffered head is **ready** per the gates.
3. If any: `select` one (live = arrival order; replay = mount order), emit it, apply `on_emit`
   (which opens gates / **mounts** a newly-referenced child on `subagent_message_sent` /
   **unmounts** a child on `subagent_stopped`), then ask `should_stop`.
4. If none: wait for an in-flight pull to deliver a head (which may open a gate). If nothing is in
   flight, every stream has terminated — end.

## Mounting / unmounting + graceful degradation

- **Mount** on `subagent_message_sent` (idempotent — a re-used child mounts once; it carries both
  the child `workflow_id` and the `from_offset` to position the cursor).
- **Unmount** on `subagent_stopped` (and when a child cursor turns out unreadable/exhausted): the
  engine closes that child's cursor and drops it. This is safe because `subagent_stopped` is emitted
  at a quiescent point — by then every one of that child's turns is drained, and a stopped
  subagent's id never reappears — so no gated event is stranded. Unmounting matters operationally,
  not just for tidiness: each open cursor holds a long-poll **update** in flight against its
  workflow, and Temporal caps concurrent in-flight updates **per workflow at 10**; releasing a
  stopped child's cursor frees its slot.
- **Graceful degradation (no retries — recovery is a fresh attach).** A subagent stream can fail to
  deliver short of normal end-of-stream — a stopped/completed child whose stream isn't yet replayable,
  or the per-workflow concurrent-update cap (`RPCError` when too many cursors poll one workflow). It
  surfaces three ways: the pull **raises** (cap), **cleanly ends** mid-turn (completed workflow), or
  **hangs** (poll against a completed child that neither yields nor returns). When the merge gives up
  on a child it (1) **releases its close gates** — a `gone` child's `subagent_reply_received` is
  treated as satisfied, so the parent's reply *and everything sequenced after it* still flow (without
  this, a dead child's never-coming `turn_end` strands the parent — the observed **hang**); (2)
  **drops the cursor** (frees its poll slot); (3) emits a non-fatal **`subagent_stream_unavailable`** marker
  (only if a turn was abandoned), stamped `agent_id == subagent_id` so a UI routes it to that
  subagent's view. The parent stream is self-sufficient, so only the child's own detail is forgone.
  A failed **root** ends the stream cleanly (no exception out of the generator, which would 500 the
  BFF). Triggers: a **raised** error or **clean mid-turn end** is reaped in `_collect_completed_pulls`;
  a **hang** is caught by a liveness backstop — when a parent `reply_received` is close-gated on a
  child, that child gets a **per-child deadline** (`stall_grace_seconds`, default 5s, configurable via
  `AgentClient.send_message` / `attach`'s `subagent_stall_grace_seconds`). The deadline is measured
  from the child's **own** last delivery — re-armed whenever it delivers, untouched by a *sibling*
  delivering — so a child is given up only when **it itself** is silent for the whole window while
  blocking. A single shared timer would instead be re-armed by any cursor's delivery, letting a chatty
  sibling defer a dead child's give-up indefinitely. That bound is **liveness, not ordering** (ordering
  stays gate/offset-driven), and applies only while close-gate-blocked, so an idle/slow live stream is
  never spuriously dropped. Once the upstream readable-completed-stream fix lands, a stopped child
  delivers its `turn_end` normally and the backstop simply stops firing.

> **Upstream constraint (not fixable here).** `subscribe()`'s `__temporal_workflow_stream_poll`
> update blocks server-side in `wait_condition` until new items arrive (or the stream detaches).
> A client that disconnects mid-poll **cannot reclaim** that parked server-side update — Temporal
> updates are not client-cancellable — so a subscribe to an **idle** child (one between turns, not
> stopped) leaves a slot held until that child's next event. Re-attaching repeatedly therefore
> *accumulates* parked polls on a live subagent. The merge degrades gracefully when the cap is hit,
> but the real mitigation is on the consumer: don't re-attach an idle session on a fixed tick
> (the example UIs poll the cheap `agent_status` query while idle and only re-attach on new work).
> An upstream `workflow_streams` fix for cancel-on-detach is in flight.

## Quiescent start (why a scalar offset is enough to resume *safely*)

`send_message` resumes at a quiescent point — the submitted turn's `turn_started` — via
`skip_until_turn_id`. A parent `turn_end(N)` proves the whole subagent subtree is idle (the turn
handler can't return while awaiting a `run_subagent_turn`, which blocks on the child's `turn_end`,
recursively), so that's a clean, empty-bracket start.

`attach` resumes from an **arbitrary** offset with **no skip** — and that's still safe without
bracket reconstruction, because the merge only ever *mounts* a child when it *emits* that child's
`subagent_message_sent`. A subagent whose turn began before the resume offset is therefore never
mounted: its events are simply absent, and its later `subagent_reply_received` (which would otherwise
close-gate forever) is released by the **unmounted-stuck give-up** — the engine sees a buffered
`reply_received` for a child that isn't mounted, knows its `turn_end` can never come, and gives up at
once. Subagents dispatched at/after the offset mount and bracket-merge normally. So no per-stream
offset vector is ever needed — just the scalar root offset plus `subagent_message_sent.from_offset`.

The merge yields a `(event, resume_offset)` pair per step; `resume_offset` is a **root-stream**
offset that advances **only on root events** — every subagent event between two root events carries
the same value (the position just past the preceding root event). A consumer records the latest and
hands it back to `attach(from_offset=...)`. Two consequences a consumer must understand:

- **Any root offset is a *valid* resume point** — the merge never produces a broken ordering or
  wedges from one. It is *not* the merged display ordinal (the cross-stream interleaving itself isn't
  a resumable position).
- **Resume is lossless only at root-event granularity.** A consumer that disconnects *mid a
  subagent's turn* and resumes will **not** re-receive the rest of that subagent's turn detail: on
  resume the root starts past that subagent's `subagent_message_sent`, so the merge never re-mounts
  it (and emits **no** `subagent_stream_unavailable` marker — it treats the detail as already
  delivered). The parent's reply and everything after it still flow. A consumer that needs every
  subagent event across a mid-turn reconnect must re-attach from `0`. Making resume *lossless*
  rather than merely safe would need a vector of offsets, one per live stream — a deferred protocol
  change, which is why the client contract below is written to avoid needless reconnects.

## Entry points

`merge_stream(client, root_workflow_id, root_from_offset, skip_until_turn_id, select, should_stop,
stall_grace_seconds)` yields `(AgentEvent, resume_offset)` pairs. `AgentClient` wraps it:
- **send_message** → `root_from_offset = reply.accepted_offset`, `skip_until_turn_id = reply.turn_id`,
  `select_live`, stop at the root's `turn_end` for that turn.
- **attach (full replay)** → `from_offset = 0`, no skip, `select_replay`, stop via status re-query
  (root idle and all turns through `current_turn` ended).
- **attach (resume)** → `from_offset = <any resume offset the previous stream handed back>`, no skip,
  `select_replay`; streams only events after it (a subagent whose turn began earlier is omitted; its
  `reply_received` released by the unmounted-stuck give-up).

> **`send_message` precondition.** It is only valid for a message that gets a **turn of its own**
> — i.e. a caller that is not already streaming. A `MidTurn.ACCEPT` message that JOINS an open turn
> has a `turn_started` *behind* its `accepted_offset`, so the skip preamble never matches: the merge
> emits nothing and the caller waits out `DEFAULT_TURN_TIMEOUT` (300s) for a single
> `AgentTurnTimeout`. Mid-turn sends go through the contract below instead. (Reachable over HTTP via
> `POST /api/chat`, which the packaged UI does not use.)

## The client contract — one stream, and how to keep it

A client renders an agent from **at most one** merged stream, and that stream is an `attach`. The
rule on every send:

1. **Send with `submit_message`** — the bare update, no stream. It returns `AgentMessageReply`.
2. **Then ensure a stream is live.** Already open → keep it, untouched. None open (never attached, or
   the last one ended at quiescence) → `attach(from_offset=<last resume offset>)`.

Three things make this the right shape rather than merely a convenient one.

**Ordering is load-bearing: send first, then ensure.** Checking for a live stream *before* the send
is racy — the open stream can stop at the current `turn_end` because its status re-query sees an idle
agent, the message not yet being admitted. Once `submit_message` returns, the message is durably
admitted and visible to `agent_status`, so no later `should_stop` evaluation can conclude "idle."

**Keeping the open stream avoids a real loss.** Re-attaching mounts nothing that opened before the
resume offset, and emits **no** `subagent_stream_unavailable` marker for it (see *Quiescent start*).
So tearing down a healthy stream to send a message silently drops the rest of any in-flight
subagent's turn. Keeping it never re-mounts, so the loss stays confined to genuine disconnects.

**`attach` already spans queued turns, so no turn bookkeeping is needed.** Its stop condition is real
quiescence (`not turn_active and not pending_turns and highest_completed_turn >= current_turn`), so a
message queued behind the open turn makes `pending_turns` non-empty and the live stream continues
straight through the current `turn_end` into the queued turn. A client never has to decide whether to
re-attach for a queued message.

And it still terminates at quiescence, which is the point of the bracket: **`turn_end` is where the
server sheds a client.** That is a resource decision, not an aesthetic one — every open cursor holds a
long-poll update in flight, Temporal caps concurrent in-flight updates **per workflow at 10**, and a
client that disconnects mid-poll cannot reclaim the parked update (see the upstream-constraint note
above). A stream held open forever leaks against a hard cap.

Design discussion, including the alternatives rejected on the way here:
[`docs/design/per-message-events.md`](../../../docs/design/per-message-events.md).
