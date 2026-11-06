# Swim lanes in the State Flow

> Status: design exploration. Stacks on [`mid-turn-steering.md`](mid-turn-steering.md) (PR #283)
> and builds on [`per-message-events.md`](per-message-events.md) (`message_id` on every
> event's envelope).

## Problem

The State Flow draws one turn as one graph: an Input card, the agent runtime box (model, tools,
Code Mode, approvals), and an Output card. The fold behind it (`flowProjection.ts`,
`AgentFoldState`) holds exactly one of each message-shaped thing: `currentUserMessage`,
`currentUserEnvelope`, `replyText`, `replyState`, `status`.

A turn now routinely has more than one message in it. Mid-turn steering makes that the normal
case: the user sends "just know I want the cheapest of everything" while `run_travel_code` is
running, the `ask` handler joins the open turn and calls `runner.steer(...)`. Every one of
that message's events lands in the same singletons, so:

- **The Input card is clobbered.** `message_accepted` with `disposition: "joined"` overwrites
  `currentUserMessage`. The turn that is running about a flight search now shows
  "does this queue?" as its input, still labelled `TURN 1`.
- **The runtime claims to be finished while it is still running.** The steering handler returns
  `TextReply(text="")`; its `message_handler_end` sets `status = "replied"` and
  `replyState = "available"` regardless of whose handler ended. The runtime header reads
  `REPLIED` above two `book_flight` calls still awaiting approval.
- **The Output card is ambiguous.** An empty reply leaves the old text (`reply.text ||
  s.replyText`); a non-empty one from a joined handler replaces the opener's.
- **Nothing shows what the steer did.** `steering_enqueued`, `steering_claimed` and
  `steering_dropped` are not folded at all, so the one causal link that matters (this message
  reached the model on that tool's result) is only visible in the chat panel and Logs.

The events already say which message caused what. The flow just ignores the field.

## What the stream already gives us

| Fact | Where | Use |
| --- | --- | --- |
| Which message caused an event | envelope `message_id` (null only on `turn_started` / `turn_end`) | route the event to its lane |
| What a message did to the turn | `message_accepted.disposition`: `opened` / `joined` / `queued` | `opened` and `joined` each open a lane; `queued` is out of scope |
| What was sent | `message_accepted.handler`, `.payload` | the lane's Input card |
| What the handler returned | `message_handler_end.output` / `message_handler_error` | the lane's state and Output card |
| A steer was queued | `steering_enqueued { steering_id, text }`, envelope = the steering message | a Steer card in that message's lane |
| A steer reached model input | `steering_claimed { steering_ids, tool_id }`, envelope = the **claiming** participant | an edge from the Steer card to the tool whose result carried it |
| A steer never reached the model | `steering_dropped { steering_ids }` just before `turn_end` | the Steer card ends dropped, no edge |

`inboundMessageText.ts` already keys frames by `message_id`, and the PR's `transcript.ts`
already keeps a `steering_id -> message` map to settle claims and drops. The flow fold needs
the same two lookups. No protocol change is needed.

## The design

### The agent is one box; handlers are lanes inside it

What the lanes show is concurrency: the harness runs several message handlers inside one
agent at once, all sharing the agent's state, and the flow should make that visible rather than
flatten it into one sequence. A turn with one handler has no concurrency to show, so it draws
exactly as today.

The agent runtime box stays the single boundary for the agent: one header, one `ACCEPTS`
row, one status. Inside it, every message handler running in the turn is a **lane**:

- The handler whose `message_accepted` said `opened` is the first lane.
- Every `message_accepted { disposition: "joined" }` adds a lane. **Always a full lane**,
  whatever the handler goes on to do: the UI cannot know at admission whether the handler
  will steer, call the model, run tools, or return at once, so it does not guess. A handler
  that returns immediately is a lane that settles immediately.
- Lanes are in **arrival order**, top to bottom, and never reorder.
- A lane holds every event whose envelope carries its `message_id`: its model calls, tools,
  Code Mode, approval satellites, steers, and reply.
- Lanes live for one turn. `turn_started` clears them with the rest of the turn's runtime
  nodes (`resetTurnTools`). Stepping across turns replays them from the fold like everything
  else.

Queued messages are out of scope for now. They keep today's treatment (queue tone and count
on the Input card) and become the first lane of the turn they open.

### Picture

```
                ┌ Parent agent runtime ─────────────────────────────── RUNNING ┐
                │ ACCEPTS  ask  set_approval…                                   │
                │ ┌╌ ask · turn opener ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ RUNNING ╌┐ │
 ┌ Input ────┐  │ ╎                                                           ╎ │  ┌ Output ┐
 │ look for  │──┼▶╎  Model ──▶ run_travel_code ──▶ book_flight  book_flight   ╎─┼─▶│        │
 │ a flight… │  │ ╎                 ▲                                         ╎ │  └────────┘
 └───────────┘  │ └╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┼╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┘ │
                │ ┌╌ ask · joined ╌╌╌┼╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ REPLIED ╌┐ │
 ┌ Input ────┐  │ ╎                  │ claimed                                ╎ │
 │ cheapest  │──┼▶╎  Steer "just know i want the cheapest…"                     ╎ │
 └───────────┘  │ └╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┘ │
                │ ┌╌ ask · joined ╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ REPLIED ╌┐ │
 ┌ Input ────┐  │ ╎                                                           ╎ │
 │ does this │──┼▶╎  Steer "does this queue?"                          QUEUED   ╎ │
 │ queue?    │  │ ╎                                                           ╎ │
 └───────────┘  │ └╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌┘ │
                └───────────────────────────────────────────────────────────────┘
```

(The second lane's steer was claimed on `run_travel_code`'s result; the third is still
waiting for the next tool result. Both handlers returned `""`, so neither lane has an Output
card.)

### Lanes are drawn only when there are two or more

Every handler is a lane in the fold, but the lane is only *drawn* when the turn has two or more:

- **One lane:** no outline, no lane label, no lane chip. The Input card wires into the runtime
  box and the box's contents are laid out as today. The only visible difference from today
  is that the bugs above are gone.
- **Two or more:** the first `joined` handler outlines every lane at once, the opener's
  included, and from then on every Input wires to its own lane.
- **For the rest of the turn.** Once a turn has shown concurrency, its outlines stay until
  `turn_started` clears the lanes, even after the joined handlers have finished. Removing
  them when a joined handler returns would erase the record that handlers overlapped.

**The opener's cards inside the box do not move when outlines appear.** The outline fits in
space the runtime box already leaves around its cards: the 28px label row in the 32px between
the box's 88px header and the first card row, the 16px inset inside the 40px side padding, and
the 16px foot inside the 40px under the last row. So a single-handler turn keeps today's exact
geometry, and when a second handler joins the outline appears around cards that stay put while
the new lane is added below. The opener's Input and Output cards, outside the box, re-centre
on its lane at that moment.

### The lane outline is light

The runtime box keeps its current border and tint; it is the agent. A lane is a delineation
*within* it, so it must never read as a second box of equal weight:

- No fill, or the faintest step off the runtime box's own fill.
- A hairline border (1px, dashed or low-contrast), from a new `--lane-border` token defined for
  both themes beside the existing flow tokens.
- A small label row at the lane's top-left: the handler name and `turn opener` / `joined`, in
  the same type as the runtime box's `ACCEPTS` row. The lane's own state chip
  (`RUNNING` / `REPLIED` / `ERROR`) at its top-right, smaller than the runtime header's.
- Lanes are separated by a 16px gap, not by heavier rules.

A lane is its own xyflow node type (`agentLane`, `AgentLaneNode.svelte`) at the runtime box's
`zIndex`, so it draws over the box's floor and under the edges and cards. Clicking it opens the
inspector with that message's input, raw envelope and reply; the flow does not raise a selected
node, so selecting a lane never covers its cards.

### Wiring

- **Input to lane.** Each lane has its own Input card in the column left of the runtime box,
  vertically centred on its lane and titled with the handler it is input to (`ask`,
  `set_approval_policy`), not a generic "Input". The edge runs from the Input card to the **lane's left
  edge** (a `target-left` handle on the lane node). It does not continue to the model or
  to any card inside the lane; entering the lane is the claim the edge makes. This applies
  only while lanes are drawn. With a single lane, today's input-to-first-runtime-node edge
  (`addRuntimeFlowEdges` with `inputSeen`) is kept. When a second lane appears, the opener's
  edge is rerouted to end at its lane's left edge.
- **Output with a single lane** stays as today: one Output card wired from the runtime's
  terminal nodes.
- **Lane to Output.** Symmetric: a joined handler gets an Output card, titled with its handler
  like the Input card, to the right of the runtime box, centred on its lane and wired from the
  lane's right edge, the moment it returns
  or raises. That includes a handler that returned `""` (every steering handler in the
  examples): its card shows what the handler returned (`{"text": ""}`), because a lane with no Output reads as a
  handler still running. A steer card is the one part of a lane that settles after its handler
  has returned, from queued to claimed or dropped.
- **Inside a lane** the existing flow edges apply unchanged (model to tool, tool to tool,
  reasoning below model, approval satellites below their call), scoped to that lane's nodes.
- **Steer claims cross lanes.** `steering_claimed` names the steering ids it took and the
  `tool_id` whose result carried them. Each named Steer card gets one edge to that tool's
  card, wherever it is, normally a lane above it. Both ends are inside the runtime box, so the
  edge never leaves the agent. The claim is shown for one beat, through the same hold that
  keeps a settled tool up (`heldIdsFromFrame`: the frame the cursor is on when reading back, a
  timed beat at the live edge): the Steer card is lit and the edge drawn. After it the card
  dims to neutral and the edge goes, because the model's own cards carry the run on from there.
  - `tool_id` is a Code Mode `run`: the edge targets that card, never a host call inside it
    (host calls never carry steering).
  - The tool card is not drawn (focus view drops settled tools), or `tool_id` is null (a
    hand-written loop claimed without a tool): the edge targets the claiming lane's Model card,
    and failing that the claiming lane's outline, so it never points at nothing.
  - Dropped: no edge; the Steer card takes the error tone with "never reached the model".

### Status

The runtime box's header status is the agent's: `RUNNING` while any lane's handler is running
or the turn is open, then whatever the turn ended as. Each lane's own chip carries that
handler's outcome. This is what removes the premature `REPLIED`: a joined handler returning
only settles its own lane.

### Fold shape

The opener keeps the fold's existing top-level fields (`currentUserMessage`, `replyText`,
`modelState`, `status`, ...), so a turn with one handler folds exactly as before. Each joined
handler gets a `LaneState`, and every event is routed by its envelope's `message_id`: one that
names a joined lane is that lane's; anything else, including frames with no `message_id`, is
the opener's.

```ts
interface LaneState {
  messageId: MessageId;
  handler: string;
  message: string;          // rendered, as currentUserMessage is
  envelope: string;         // as currentUserEnvelope is
  handlerState: "joined" | "running" | "replied" | "error";
  currentModel: string;
  modelState: string;
  reasoningState: string;
  reasoningDetail: string;
  replyText: string;
  replyIsData: boolean;
  replyState: string;
  outputSeen: boolean;
}

interface AgentFoldState {
  // ...existing fields, the opener's, unchanged...
  currentHandler: string;                    // the opener's handler, for its lane label
  handlerState: LaneState["handlerState"];   // the opener's handler, for its lane chip
  lanes: Map<MessageId, LaneState>;          // insertion order = arrival order
  steers: Map<SteeringId, SteerRuntime>;     // text, queued/claimed/dropped, carrying tool_id
  nodeLane: Map<RuntimeNodeId, MessageId>;   // the joined lane a runtime node was first marked in
}
```

- A joined lane's own lifecycle, model, thought and reply events are folded into its
  `LaneState`. Everything else (tools, approvals, callbacks, steering) goes through the shared
  code and is filed under the lane by `nodeLane` when its node is first marked. Tools stay in
  the one `s.tools` map, so focus view, retry collapsing, linger and the settled-tool drop work
  in every lane without a per-lane copy.
- Joined lanes' own cards are `input:<messageId>`, `output:<messageId>`, `model:<messageId>`
  and `reasoning:<messageId>`; steer cards are `steer:<steeringId>`; lane outlines are
  `lane:opener` and `lane:<messageId>`.
- `turn_started` clears `lanes`, `steers` and `nodeLane` with the turn's tools. Checkpoints
  copy them with the rest of `AgentFoldState`.

### Layout

The runtime box becomes a vertical stack of lanes under its header:

- Each lane is laid out on its own with the existing `runtimeLayoutFor` over that lane's
  nodes, so a lane that runs tools grows exactly as the runtime box does today.
- A lane is as tall as what it holds, including its approval satellites. A lane with nothing in
  it (a handler that only changed state and returned) shrinks to its label row.
- A joined handler's Input, Output and Steer cards are compact (130px); the opener's Input and
  Output stay full size.
- While outlines are drawn, each lane's Input and Output cards are centred on the lane, and the
  lane's side ports sit at its middle. Lanes are spaced only as far apart as the cards centred
  beside them need to clear each other.
- Lanes are as wide as the widest lane; the runtime box is that width plus its padding, and as
  tall as its header plus the lanes and gaps.
- A new lane only adds height below the existing ones, so the lanes already on screen do not
  move when a message joins. `AutoFitView` fits the larger bounds.
- The runtime box reads `RUNNING` while any joined handler is still running, even after the
  opener's handler has replied.

### Subagents

Lanes are per graph. A message sent to a child mid-turn shows as a lane inside the child's own
runtime box (the embedded graph under the parent's tool container), because the child's events
carry the child's `message_id`s. A parent's steer never reaches a child (see
`mid-turn-steering.md`), so no claim edge crosses a graph boundary.

## Implementation

- `typescript/apps/ui/src/lib/state/flowProjection.ts`: routing, `LaneState`, steering, lane
  layout and edges.
- `typescript/apps/ui/src/lib/components/flow/AgentLaneNode.svelte`: the outline; registered as
  `agentLane` in `AgentStateFlow.svelte`, which also defines `--lane-border`, `--lane-fill` and
  `--edge-steer`.
- `typescript/apps/ui/src/lib/state/nodeLogRows.ts`: "view in logs" for a joined lane's cards,
  held to that lane's `message_id`.
- `typescript/apps/ui/src/lib/state/flowSwimLanes.test.mjs`: the behaviour above.
