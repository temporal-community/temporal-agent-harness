# The AgentEvent history and Workflow Streams

How the harness's AgentEvent history is carried, made durable, and replayed — grounded in the
`temporalio.contrib.workflow_streams` primitive and how the harness uses it. This doc is the
*mental model + durability* companion to
[`event-stream-and-storage.md`](event-stream-and-storage.md), which covers the wire mechanics, the
client-side merge, and where large payloads are stored. Start here for "what is a Workflow Stream
and why is the AgentEvent history durable"; go there for "how the merge and storage work."

## The one-sentence model

The **harness event history** — turns, model interactions, tool calls, approvals — is a log of
typed `AgentEvent`s carried on a **Workflow Stream**, a generic durable streaming construct from
`temporalio.contrib`. The history *is* the stream; the stream is *how* the history is durable and
replayable.

## Workflow Streams are a general construct, not an agent feature

`WorkflowStream` (`temporalio.contrib.workflow_streams`) gives any workflow a **durable,
offset-addressed event channel** for keeping outside observers updated on the progress of the
workflow and its activities (its README's words). It is payload-agnostic — the same construct
serves UI feeds for long-running agents, status during payment/order processing, or progress from
data pipelines.

You carve a stream into **topics**, and *each topic is parametrized by a type*. The type defines
both **what** travels and **at what granularity** — one publish is one value of that type:

```python
stream.topic("turn_events", type=AgentEvent)    # the harness's channel
stream.topic("tokens",      type=list[str])     # a hypothetical raw-token channel
```

The harness defines exactly **one** topic, `TURN_EVENTS_TOPIC = "turn_events"`, typed to
`AgentEvent` (`agent_protocol/events.py`). That single choice — *this topic, this type* — is what
turns a generic log into "the harness event history." Pick a different type and the same construct
is a token stream or a progress-bar stream.

### Why it lives in `temporalio.contrib`, not core Temporal

Workflow Streams are **not** a new durable-execution primitive. They are a Python-SDK-level pattern
assembled from primitives that already exist — "*Signals carry publishes, Updates serve long-poll
subscriptions, and a Query exposes the current global offset*" (the package README). Because it is
(a) built on existing primitives rather than a server feature, and (b) still evolving (the README
marks it **experimental**, and the harness notes in-flight upstream fixes — see
[Retention and limits](#retention-and-limits)), it sits in `contrib`, outside the frozen,
cross-SDK core stability contract. `contrib` is Temporal's home for officially-supported-but-not-yet-
frozen constructions, alongside the framework integrations (`openai_agents`, `pydantic`, …).

## Stream vs. query: two shapes of "get data out"

A Workflow Stream is the ergonomic answer to a job queries serve poorly: **observing a workflow's
output as it unfolds.** It does not replace queries for snapshots — the harness uses both.

| | **Query** (`agent_status`, `agent_interface`) | **Workflow stream** (`turn_events`) |
|---|---|---|
| Direction | Pull — you ask, once | Push/subscribe — items arrive as produced |
| What you get | A snapshot of current state | The sequence itself — every item, in order |
| Time | "What is true *right now*?" | "What happened, and what happens *next*?" |
| History | None — recomputes from current state | Append-only, offset-addressed, replayable |
| Watching change | Poll (can miss anything between polls) | See every item; a late joiner replays from an offset |

The harness deliberately exposes some facts **both** ways. A pending tool approval is published as a
`tool_approval_requested` stream event *and* is readable via `agent_status.pending_approvals` — so a
live subscriber learns of it from the stream, while a client that attaches late reads it from the
query snapshot without replaying the whole log (`events.py`, `agent_workflow.py`).

## Why the AgentEvent history is durable

Every publish is a **Temporal Signal**, so it lands in the workflow's event history. That yields the
full durable-execution guarantee:

- **Crash- and replay-safe.** On worker crash/redeploy, the workflow replays its event history,
  re-applies every publish Signal in order, and **deterministically reconstructs the in-memory
  log.** The stream is a *projection* of the workflow's Temporal event history, not separate storage.
- **Exactly-once, ordered.** The library adds batching + dedup (by publisher id / sequence) on top
  of Signals.
- **Offset-addressed and resumable.** Every item has a global offset; a consumer reads
  `from_offset=N` and reattaches from where it left off.

### Consequence: the UI holds no history of its own

Because the history lives in the workflow, the packaged UI is a **stateless viewer**. On (re)connect
it calls `attach(session_id, from_offset=0)` (`web/app.py`) and the workflow **replays the whole
event history** from its durable stream — which is exactly what backs the UI's play/pause replay.
Kill the browser, restart the web server: reattach and it rebuilds. The client is handed a
`resume_offset` with each item (`agent_client.py`) so a reconnect can resume *without* re-replaying
everything. As the client module puts it: "*resumable via a stream offset so that disconnects don't
lose events.*"

### Determinism of `truncate` on replay

`truncate(up_to_offset)` (`_stream.py`) trims a consumed prefix to keep the log bounded. It is a
**pure function of an explicit offset argument** and the current log state — no wall-clock, no
randomness. Its trigger (an ack-offset Update / the continue-as-new path) is itself a recorded
history event. So on replay the same publishes and the same `truncate` calls re-run in the same
order, reconstructing the **identical** trimmed log: same base offset, same remaining entries, same
monotonic global offsets, same `TRUNCATED_OFFSET_ERROR` for a below-base poll. (The `workflow.now()`
calls in `_stream.py` are Temporal's deterministic time and drive only dedup-state idle expiry, never
the truncation boundary.) The original publish Signals for trimmed events remain immutably in raw
history, but replay re-applies the truncate — so reconstruction yields the post-truncation logical
stream, not the resurrected entries.

## How the harness uses the stream

- **One stream per agent**, constructed in the agent's `@workflow.init` (root *and* every subagent
  has its own; subagent streams are never mirrored onto the parent — see
  [`unified-subagent-event-stream.md`](unified-subagent-event-stream.md)).
- **One topic**, `turn_events`, typed to `AgentEvent`. The `AgentEvent` envelope carries routing
  metadata (`agent_id` / `turn_id` / `turn_number` / `timestamp`) that only the harness can stamp;
  producers build only the semantic payload (e.g. `ReplyDelta(text=…)`). Vocabulary lives in
  `agent_protocol/events.py`.
- **Two publish paths onto the same topic:**
  - *In-workflow* — `_pub` → `WorkflowTopicHandle.publish` (`agent_workflow.py`): lifecycle events,
    the approval cascade, inline-tool `tool_start`/`tool_end`, subagent parent-side markers.
  - *Out-of-workflow* — `AgentWorkflowRunner.publisher_from_activity` → `WorkflowStreamClient`
    (delivered as the publish Signal): Gemini/OpenAI streamed `reply_delta`, `model_interaction_*`,
    and activity-tool `tool_start`/`tool_end`. Raw provider tokens are folded into semantic
    `AgentEvent`s *inside* the model-call activity — the lowest-level thing that ever crosses the
    activity→workflow→client boundary is already an `AgentEvent`, never raw bytes.
- **Consumers** subscribe by `workflow_id` (`WorkflowStreamClient.create(...).subscribe(...)`); the
  UI-facing "stream" is a client-side merge of the whole agent tree — see
  [`event-stream-and-storage.md`](event-stream-and-storage.md).

## Retention and limits

Durable does **not** mean retained forever, or free:

- **Bounded by truncation.** After `truncate`, a poll for a discarded offset gets an
  `ApplicationError` (`TRUNCATED_OFFSET_ERROR`); replay-from-offset works only back to the current
  base, not to the beginning of time.
- **Spans runs via continue-as-new — your responsibility.** `get_state` / `continue_as_new`
  (`_stream.py`) carry stream state into the next run; dedup state for publishers idle beyond a grace
  period (default 15 min) is dropped in the handoff.
- **Completed-stream replay is a known gap.** While the workflow is live, replay-by-offset is solid;
  once it completes/detaches, cold replay isn't fully there yet — an upstream `workflow_streams` fix
  is in flight (`events.py`, `stream_merge/README.md`). In normal operation the session workflow is
  long-running (it parks awaiting the next message), so it *is* alive and replay works.
- **Tuned for progress feeds, not real-time.** ~100ms per roundtrip; "*not designed for … real-time
  voice*" (README). Cost scales with durable batches (each is a real Signal in history), not with
  tokens.
- **Experimental.** The API is not frozen (this is why it's in `contrib`).

## See also

- [`event-stream-and-storage.md`](event-stream-and-storage.md) — wire mechanics, the client-side
  merge, and where the event log / large payloads are stored.
- [`unified-subagent-event-stream.md`](unified-subagent-event-stream.md) — per-agent stream isolation
  and how a fleet's streams merge into one.
- [`human-in-the-loop-tool-approvals.md`](human-in-the-loop-tool-approvals.md) — the approval events
  that ride this stream.
- [`what-the-harness-adds.md`](what-the-harness-adds.md) — where the AgentEvent history fits among
  the harness's four pillars.
