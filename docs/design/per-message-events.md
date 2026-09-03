# Per-message events: the stream needs to encode messages, not just turns

> Status: **design in progress.** Follows on from
> [`unified-message-dispatch.md`](unified-message-dispatch.md), which is implemented. That
> change made a turn the interval the agent is non-idle, so several messages can now share
> one turn — and the event vocabulary has not caught up. Nothing here is built yet.
>
> The vocabulary and the envelope change are settled (see **Settled**). The
> admission-ordering question that used to block implementation is **resolved by dissolving
> it**: it was a question about a stream shape no consumer should ask for. See
> [**Resolved: the per-turn stream is not the mid-turn surface**](#resolved-the-per-turn-stream-is-not-the-mid-turn-surface).
> Nothing in that resolution changes the protocol, so the work here is unblocked and
> independent of it.

## The problem

Before mid-turn messages, one message *was* one turn, so the stream could encode a message
as a field on the turn: `turn_started.user_message`. That identity is gone. A
`MidTurn.ACCEPT` message joins an open turn, and the stream currently says nothing about it
at all — it appears only as an extra `reply` inside a turn whose `turn_started` names a
different message.

Three concrete symptoms, in increasing order of how much they hurt:

1. **A mid-turn message is invisible.** Nothing in the stream records that it arrived, what
   it was, or that it was accepted. A debug UI cannot render the input at all.
2. **Replies cannot be paired with messages.** `AgentReply` / `AgentError` carry no message
   identity — only the envelope's `turn_id`, which is no longer unique per message. With two
   participants in a turn, a consumer sees two `reply` events and cannot tell which message
   produced which. There is also no guarantee the turn's *opening* message replies last, so
   ordering is not a workaround.
3. **Streaming text is unattributable.** `ReplyDelta` / `ThoughtSummaryDelta` have the same
   problem, so two handlers streaming concurrently interleave into one indistinguishable
   text stream. `tool_start` / `tool_end` say *which tool call* (`tool_id`) but not *which
   message* caused it.
4. **A parent agent can get the wrong subagent reply.** The only symptom that is not a
   rendering gap. `_consume_child_turn` (`subagent_activities.py:196-206`) filters the
   child's stream by `turn_id` and does `output = payload.output` on every `REPLY` it sees —
   **last reply wins**. A shared child turn carries one `reply` per participant under one
   `turn_id`, so `run_subagent_turn` can hand the parent's model *another message's* output
   as its tool result, and raise *another participant's* `AgentError` as this tool call's
   failure. Not reachable through the bundled examples (their `ACCEPT` handlers are all
   `model_callable=False`), but reachable by construction: `model_callable` **defaults to
   `True`**, and `SubagentToolPolicy.allow_only()` deliberately overrides a `False` hint.
   This one is silent corruption of a tool result, so it sets the priority for the whole
   note.

The vocabulary already solved this shape once: `tool_id` correlates
`tool_start` → `tool_end` / `tool_error` / `tool_progress_delta`. Messages never needed the
analogue, because `turn_id` was one. **`message_id` is the missing primitive.**

## Settled

### `message_id` goes on the envelope

```python
AgentEvent{ agent_id, turn_id, turn_number, message_id | None, timestamp }
```

Envelope-side rather than in event bodies, so *every* event emitted during a message's
dispatch is attributable for free — deltas, tool lifecycle, approvals, subagent brackets —
exactly the way `turn_id` works today. Body-side would mean adding the field to eight event
types and still missing anything added later.

`message_id` is `None` for events that genuinely are not about one message: `turn_started`,
`turn_end`, and the entry-carried approval/callback resolutions that
`_publish_approval_resolved` / `_publish_callback_resolved` deliberately publish outside any
participant (an update handler driving a policy cascade is not bound to one message any more
than it is to one turn).

The id is minted in the synchronous admission prologue and returned on
`AgentMessageReply.message_id`, so a sender can correlate its own message without parsing the
stream.

### The vocabulary

```
message_accepted      { type, payload, disposition }   # admission
message_handler_start { }                              # the @agent.accepts method begins
message_handler_end   { output }                       # it returned
message_handler_error { message }                      # it raised
turn_started          { }                              # pure lifecycle bracket
turn_end              { }                              # pure lifecycle bracket
```

`disposition: "opened" | "joined" | "queued"` — what this message did to the turn.

Named `message_handler_*` rather than `handler_*`: "handler" alone is overloaded (Temporal
has update/query/signal handlers), and explicitness beats matching `tool_start`'s terseness.

**Why admission and execution are separate events.** An earlier draft folded them together,
publishing only `message_accepted{pending}` at admission and letting a consumer infer "it
started running" from the later `turn_started` of the turn it occupies. That was rejected:

- It is an inference joined through `turn_id`, valid only because `open_next_turn` pops
  exactly one message per turn. The refcounted turn model makes draining several queued
  messages into one turn a natural extension, and that would break every consumer's
  inference silently, with no protocol change to signal it.
- It never worked for joins at all — a join emits no `turn_started`, so "started running"
  rested on the separate assumption that joins run immediately.
- Two events make **queue latency directly observable** (accepted-at vs started-at, per
  message), which is precisely what a debugging UI wants and which the folded version left
  to be reverse-engineered from turn timestamps.

### What this replaces

- `turn_started.user_message` — removed; `turn_started` becomes an empty bracket.
- `MessageQueued` — deleted. It is `message_accepted{disposition: "queued"}`.
- `AgentReply` (`reply`) → `message_handler_end`.
- `AgentError` (`error`) → `message_handler_error`.

The last two are renames rather than additions, which is churn in the UI's transcript and
replay-log for no functional gain — but with `message_handler_*` as the family, leaving
`reply`/`error` outside it would make the pairing invisible in the very place a reader looks
for it. `message_handler_end` / `_error` also mirrors the established `tool_end` /
`tool_error` pattern: one start, exactly one terminal.

## Resolved: the per-turn stream is not the mid-turn surface

The question was where `message_accepted` sits relative to `turn_started`. It fires in the
admission prologue, **before** the turn it opens, but `AgentClient.send_message`'s merge
starts at `accepted_offset` and discards everything up to this turn's `turn_started` — an
anchor chosen deliberately because it is quiescent (no subagent bracket can be half-open
there). So a per-turn stream drops the very event that says what the message was. Three
candidates were on the table: embed the opener's payload into `turn_started`; keep
`message_accepted` standalone and let the per-turn stream omit it; or move the merge anchor
back to `message_accepted` and give up quiescence.

**The question presumed the wrong consumer.** A mid-turn message is not streamed through a
per-turn stream of its own, so there is no per-turn stream for `message_accepted` to be
missing from. Candidate 2 — always standalone — is adopted, and the cost the earlier draft
charged it with evaporates: that cost was "a `send_message` caller does not observe *other*
messages joining its turn," and under the contract below no such caller exists.

### What a joining `send_message` actually does today

Measured, because the failure mode decides how much this matters. Hold a turn open, send a
`MidTurn.ACCEPT` message through `AgentClient.send_message`, then release the opener so the
turn genuinely ends:

```
next_expected_turn while busy = 2
yielded: AgentTurnTimeout
TOTAL ITEMS: ['AgentTurnTimeout']
```

Nothing else, ever. `cursor.py:127-135` discards every event while `_skipping`, and the
`turn_started` it is looking for sits *behind* `accepted_offset` for a join — so the preamble
never un-skips, nothing is emitted, `should_stop` never fires, and the caller waits out
`DEFAULT_TURN_TIMEOUT = 300.0` for a single timeout. Not a degraded stream: a five-minute
silent dead end.

That is the real signal. `send_message` is not a mid-turn surface and never was; it just
failed to say so.

### The client contract

A client holds **at most one** merged stream, and it is an `attach`:

1. **Every send is `submit_message`** — the bare update, no stream. It returns
   `AgentMessageReply` and nothing else.
2. **After the send returns, ensure a stream is live.** If one is already open, keep it,
   untouched. If none is (never opened, or it ended at quiescence), `attach` from the last
   resume offset the previous stream handed back.
3. **`send_message` is only for a caller holding no stream** — a script or connector doing
   one request/response. Such a caller is never mid-turn, so it never joins.

Step 2's ordering is load-bearing and not interchangeable. Checking *before* the send is
racy: the open stream can stop at the current `turn_end` because the status re-query at that
instant sees an idle agent — the message is not admitted yet. Once `submit_message` returns,
the message is durably admitted and visible to `agent_status`, so every subsequent
`should_stop` evaluation sees the work and declines to stop. **Send first, then ensure.**

### Keeping the open stream is a fix, not a tidiness preference

The packaged UI already sends via `submit_message` and streams via `attach`
(`agentRun.svelte.ts:884-911`), but it re-attaches *unconditionally* on every send — and
`#beginStream()` (`:422-435`) aborts the live stream to do it. A re-attach starts the merge at
the resume offset with **no skip**, so a subagent whose turn began earlier is never mounted:
its remaining detail is absent and, per `stream_merge/README.md`, **no
`subagent_stream_unavailable` marker is emitted**, because the merge treats that detail as
already delivered.

So today, sending any mid-turn message while a subagent is running **silently loses the rest
of that subagent's turn** in the one UI whose job is to show everything. Keeping the stream
never re-mounts anything, so the loss does not occur. It reverts to what it should be: a
property of genuine reconnects, where it is unavoidable, rather than something a control
message triggers.

### `attach` is sufficient without turn bookkeeping

The property that makes step 2 enough. `attach`'s stop condition (`agent_client.py:601-606`)
is real quiescence, not a turn count:

```python
not status.turn_active and not status.pending_turns
and highest_completed_turn >= status.current_turn
```

A queued message makes `pending_turns` non-empty, so when the current turn's `turn_end`
arrives the re-query answers "not idle" and the live stream **continues into the queued
turn**. "Keep watching until my queued message's turn ends" therefore needs no client-side
tracking at all — and the stream still hangs up at true quiescence, which is the whole point
of the bracket: it is where the server can shed a client. That matters concretely, not just
architecturally — every open cursor holds a long-poll update in flight, Temporal caps
concurrent in-flight updates **per workflow at 10**, and a client that disconnects mid-poll
cannot reclaim the parked update. A stream that never ends is a leak against a hard cap.

### Rejected, with reasoning

Recorded because each is plausible enough to be re-proposed.

**A `stream_start_offset` on the reply, anchoring a join's merge at the joined turn's
`turn_started`.** It works — verified: the merge then yields `turn_started → reply → reply →
turn_end` for a shared turn, and the runner can supply the number, since `_publish_to_topic`
appends to `_log` synchronously so `_on_offset()` read immediately before the `_pub` in `run()`
is the offset `turn_started` lands at. It was rejected because it makes a joining caller
replay the whole turn prefix it has usually already rendered, and because the field's necessity
is disjoint across dispositions — the joined turn's `turn_started` is knowable only for a join,
while for an opened or queued message that turn has not started yet — so it is one value with
two meanings, existing only to serve a stream shape the contract above removes.

**Filtering a joining caller's stream by its own `message_id`.** The obvious repair for the
replay, and wrong. A mid-turn message is typically a trivial control message (swap the model),
so the interesting content — the model loop, the tool calls, the deltas — belongs to the
*opener's* `message_id`. Filtering delivers the caller the least interesting slice and forces
a second stream to get the rest. No UI wants per-message streams; a debug UI wants everything,
in one connection.

**Splitting the merge into read-start / emit-start / stop** — read from the turn's
`turn_started` so children mount, emit only past the client's last-seen offset, stop at the
message's `turn_end`. The best of the rejected designs, and cheap in the engine: `merge.py:219-234`
already `yield`s *before* calling `gates.on_emit`, and mounting, unmounting and gate-opening are
all driven by `on_emit`, so suppression is "skip the yield, keep the `on_emit`."

Two things sank it, both worth keeping written down:

- **It is not expressible for subagent events.** `resume_offset` advances only on root events
  (`merge.py:217-218`) and the SSE frame carries no per-source offset (`web/app.py` `_yield_item`),
  so "emit past last-seen" is well defined for root events and undefined for child ones. When the
  merge re-mounts a child at its `from_offset` it re-reads that child's turn from the start, with
  nothing to say which of those the client saw. Fixing that needs the per-stream offset vector
  (see **Also needs care**), which is out of budget.
- **Do not implement it with the existing skip preamble.** `cursor.py:110-113` discards skipped
  events "WITHOUT being emitted **or recorded** (so any prior-turn subagent brackets in that tail
  never enter the merge)" — deliberately excluding them. This design needs the exact opposite:
  record everything, withhold only the yield. Same-shaped mechanism, opposite intent; conflating
  them silently unmounts the subagents the design exists to capture, and fails quietly.

### What this leaves undone

Neither is a prerequisite for the vocabulary in **Settled**, and neither is fixed by the
contract above.

- **`send_message` must stop hanging on a join.** The precondition is now documented, but a
  violation still costs 300 seconds of silence. `/api/chat` (`web/app.py:287`) calls
  `send_message`, so it is reachable over HTTP even though the packaged UI does not use it. A
  fast-fail needs the reply to say that the message joined — `pending=False` covers both a join
  and an idle open — which is `disposition` from **The vocabulary**, on `AgentMessageReply`
  alongside `turn_number`. Cheapest correct version of this note's work.
- **The client contract needs to be the code.** `agentRun.svelte.ts:911` re-attaches
  unconditionally; it should attach only when no stream is live.

## Also needs care

- **Per-message delta attribution is the expensive part.** Envelope-side `message_id` means
  the id must be available wherever events are published, and activity-side publishing goes
  through `TurnStreamContext`. That implies a `message_id` on that carrier, a `ContextVar`
  alongside `_CURRENT_RUNNER` set per participant task, and a touch to every SDK plugin that
  threads the context (the Gemini and OpenAI streaming paths). A cheaper staging is to land
  the four events with `message_id` in their bodies — which fixes symptom 2 — and defer
  envelope-side attribution, leaving symptom 3 until a follow-up.
- **Consumers to migrate:** `ui/src/lib/state/transcript.ts`, `replayLog.ts`,
  `agentRun.svelte.ts` (all key on `turn_started.user_message`, `reply`, `error`,
  `message_queued`), `harness/agent_client.py`'s terminal detection,
  `subagent_activities.py`'s `_consume_child_turn` (symptom 4 — it must select the `reply`
  carrying *its own* `message_id` instead of the last one on the turn, and ignore an
  `AgentError` belonging to another participant), and the Nexus `turnEventToDelta` mapping in
  the Go connector — which is already pending the deferred Nexus work.
- **Deferred: a per-stream offset vector.** A resume position is a single *root* offset today,
  so a subagent turn that opened before it is never re-mounted and its remaining detail is
  silently absent — no marker, no error (`stream_merge/README.md`, "Quiescent start"). Making
  resume lossless needs a vector of offsets, one per live stream, which is a protocol change
  well beyond this note's budget. Accepted for now because the client contract under
  **Resolved** removes the frequent trigger, leaving the loss only on genuine disconnects. The
  read/emit/stop split rejected above becomes worth revisiting once the vector exists.
- **`expected_turn` interacts with this.** A join does not advance the turn counter, so a
  client must set its next `expected_turn` from `AgentMessageReply.turn_number + 1` rather
  than counting sends. Now that the reply will also carry `message_id`, this is a good moment
  to make the reply the documented source of truth for both.
