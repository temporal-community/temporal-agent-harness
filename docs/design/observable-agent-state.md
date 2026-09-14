# Observable agent state

How an agent's own working state — a plan, a todo list, a scratchpad, whatever the workflow
author declares — becomes something a client can track exactly, without polling and without
missing anything between polls.

**Status:** exploration branch. The state layer, its stream events, the console pane that
renders them, and one example agent on top of them (the Monty trip board) are in. Nothing that
predates the branch has been migrated.

## The gap this fills

[`agentevent-workflow-stream.md`](../internal/agentevent-workflow-stream.md) draws the line
between the two shapes of "get data out":

| | **Query** (`agent_status`) | **Workflow stream** (`turn_events`) |
|---|---|---|
| What you get | A snapshot of current state | The sequence itself — every item, in order |
| Watching change | Poll (can miss anything between polls) | See every item; a late joiner replays |

Agent *state* wants both columns at once. You want to know what is true right now, **and** you
want every change, in order, without polling. A query gives you the first. The event stream
gives you the second, but only for facts someone remembered to publish an event about.

Observable state closes that: the author declares state as an ordinary Pydantic model, and
every change to it arrives on the existing event stream as RFC 6902 JSON Patch ops. A consumer
holds one snapshot, applies ops, and is exactly in sync — the same way the UI already holds no
event history of its own.

## What the author writes

All of it:

```python
@workflow.defn(name="PlannerAgent")
@agent.defn
class PlannerAgent:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(config, stream=WorkflowStream(), ...)
        self._plan = self._runner.state("plan", PlanState())   # <- the entire opt-in

    @agent.accepts
    async def plan(self, message: TextMessage) -> TextReply:
        with self._plan.mutate() as d:
            d.goal = message.text
            d.steps.append(Step(name="research"))
            d.steps[0].done = True
        return TextReply(text=f"{len(self._plan.current.steps)} steps")
```

That is the complete contract. The author never picks a topic, builds an event, chooses a
granularity, or decides when to publish. `AgentWorkflowRunner.state()` publishes an
`AgentStateSnapshot` at registration and one `AgentStatePatch` after every `mutate()` block
that actually changed something. A block that touches nothing publishes nothing and burns no
version.

The mutation above produces:

```json
{"op": "replace", "path": "/goal",           "value": "ship the feature"}
{"op": "add",     "path": "/steps/-",        "value": {"name": "research", "done": false}}
{"op": "replace", "path": "/steps/0/done",   "value": true}
```

Paths are exact. `d.steps[0].done = True` is one op against one field, not a re-send of the
list — so a UI can highlight the field that moved rather than re-rendering the plan.

## How it works

State is held as deeply immutable Pydantic values. `mutate()` hands out a *draft*: a real
instance of the author's own class (a generated `Draft[PlanState]` subclass, so
`isinstance` holds and mypy sees `PlanState`) in a mode where writes are recorded. On clean
exit the harness builds the next immutable value — sharing every untouched subtree with the
previous one by identity — and hands the ops to the publisher. There is no diffing and no
reconciliation pass: cost is proportional to what was touched, not to the size of the state.

Outside a draft, every write raises `FrozenError` naming the fix. This is Immer's model
(`produce(state, draft => ...)`) plus React's "state is a value; the setter is the only place
it changes."

`temporal_agent_harness/harness/state/` is a **leaf package** — stdlib and pydantic only, no
imports from anywhere else in `harness` — for the same reason `stream_context.py` is: the
runner, the protocol and offline tests can all depend on it without a cycle. It does not know
Temporal exists. The runner translates its `StateSnapshot`/`StatePatch` dataclasses into the
`AgentStateSnapshot`/`AgentStatePatch` stream payloads.

## Why one topic, not two

State events ride `turn_events` as two new `AgentEventType` members rather than a second topic:

- **The plumbing already exists.** Attach/replay in `web/app.py`, resume-by-offset in
  `agent_client.py`, and the whole-tree merge in `stream_merge` all work on `turn_events`
  unchanged. A second topic would need every one of them taught about it.
- **Ordering is the feature.** On one topic a state patch is ordered *against* `tool_start`
  and `reply_delta`, so a consumer can see where in a turn the state moved. Two topics have
  no cross-order at all.
- **The envelope is already right.** `AgentEvent` stamps `agent_id` / `turn_id` /
  `turn_number` / `timestamp`, which is exactly the attribution a patch wants.

State registered in `@workflow.init` has no turn to belong to, so its snapshot publishes with
`turn_number=0`, following the operator-command convention.

## Determinism

Publishing happens in-workflow, so every op must be a pure function of the mutations made.
Two places where that needed care:

- **Set ordering.** Sets serialize as JSON arrays, and CPython set iteration order depends on
  string hashing — which varies per process. A frozen set here iterates in *sorted* order when
  its elements are orderable, so the same mutation yields the same array on any worker.
  Without this, replay on a different worker could produce a different patch payload.
- **Commit rebuilds.** A dirty node is rebuilt with `model_construct` when the class declares
  no validators and no validation-affecting config, and with `model_validate` otherwise (so a
  `field_validator` still fails the commit). Both paths are deterministic; the choice is made
  once at class-definition time, not per commit.

## What is not done

- **`AgentStatus` is untouched.** The obvious next step is making some of it observable state
  so the UI stops polling it, but that edits the runner's core and the Svelte client.
- **Subagents.** A subagent gets its own runner and its own stream, so `state()` works there
  already. The console keys its documents per agent, so a subagent's state renders beside the
  root's — but nothing in the *canvas* draws it.

  One thing this did break, and it is fixed rather than outstanding: registering state in
  `@workflow.init` puts a `turn_number=0` event at offset 0 of a subagent's stream, and
  `stream_merge`'s open gate held every child event whose turn had no bracket. Nothing can ever
  open a bracket for turn 0, so that was not a delay but a strand — and a held event stays its
  cursor's head, so the child's whole stream queued behind it. Turn-0 child events are now
  exempt from the open gate, which is also what operator commands on a subagent needed.
- **Root must be a `HarnessState`.** `runner.state("tags", set())` is not supported.
- **No op coalescing.** `d.x = 1; d.x = 2` emits two ops.

## In the console

An AGENT STATE pane renders the fold. It is a projection over the frames up to the playhead,
like every other reading in the console, and that is the whole of why it is one: a viewer that
applies each patch as it arrives is correct while you are watching live and meaningless the
moment anyone scrubs, because an arrival is not a position. Park the transport on a
`state_patch` and the pane shows the state as of that commit, with that commit's own paths
marked. Step back and the marks move back with you.

The marking is exact for the same reason the ops are: every value in the rendered document
carries its own RFC 6901 pointer as `data-path`, so `replace /steps/0/done` marks that token
rather than the row, the object, or the document. A `remove` marks the container it emptied —
the node it names is the one thing no longer there to mark — and the ops themselves are listed
above the document, which is where a removed value can still be read.

Two cases the pane has to be honest about rather than paper over:

- **No snapshot on the stream.** A state's snapshot is published once, when the agent registers
  it, so a stream attached from a later offset carries only the patches — ops against a
  document this client has never seen. Folding them into `{}` would render a plan the agent
  never had, so the pane says what happened instead.
- **An op that cannot land.** The document keeps whatever the ops before it left, because that
  is a state the agent really passed through, and says the patch broke.

`state_snapshot` and `state_patch` also draw rows in the LOGS pane. That is what cashes in the
one-topic decision above: a commit is ordered against the `tool_start` and `reply_delta` around
it, and a log that skipped them would quietly assert nothing happened between two model calls.

## In an example

`examples/monty/trip_board.py` is the first agent to use this: a running board of the trips the
travel agent is collecting — description, flights, hotels, and what is left to do. It is worth
reading as the shape a real opt-in takes, and for two things that fall out of it:

- **The tools are inline, not activities.** The board is the agent's own notes, so every tool
  is an `@agent.tool_defn` running in the workflow, writing to a `StateRef` the workflow owns
  and the harness publishes. Its ancestor is `examples/callback_tools/coding_agent/tools.py`,
  whose `todowrite` does the same thing into a plain `Injected[list]` — identical shape, except
  that nothing outside the workflow can see it change.
- **A commit is the consistency boundary.** `record_booking` appends the flight and ticks the
  checklist item it closes inside one `mutate()`, so no consumer ever observes a board showing
  a booked flight next to an outstanding "book the flight".

The board rides in the same Code Mode sandbox as the travel tools, so a script books and
records without a round trip through the model, and the `StateRef` is an `Injected[...]`
parameter — hidden from the model and from the generated stubs, so a script names a trip and
never the state it lives in.

## Reference

- Next: [`rendering-agent-state.md`](rendering-agent-state.md) — how an author says what a piece
  of state *means*, so a client can draw it as a checklist or a table rather than as JSON
- Layer: `temporal_agent_harness/harness/state/`
- Wire types: `AgentStateSnapshot` / `AgentStatePatch` in `agent_protocol/events.py`
- Opt-in: `AgentWorkflowRunner.state()` in `agent_workflow.py`
- Console: `ui/src/lib/state/agentState.ts` (the fold), `ui/src/lib/state/jsonPatch.ts` (the
  applier) and `ui/src/lib/components/agent/AgentStatePanel.svelte` (the pane)
- Example: `examples/monty/trip_board.py`, registered in `examples/monty/workflow.py` and
  `examples/monty/conversational_workflow.py`
- Tests: `tests/state/` (the layer, incl. a hypothesis property test),
  `tests/harness/test_observable_state.py` (end-to-end through a real workflow),
  `tests/examples/monty/test_trip_board.py` (the example, end-to-end: a script drives the board
  and the ops replay into what the agent holds) and
  `ui/src/lib/state/{agentState,jsonPatch}.test.mjs` (the fold, replayed over the console's own
  mock session)
