# Turn interrupt: drain the open turn without closing the agent

> Status: **proposed**. Builds on [`unified-message-dispatch.md`](unified-message-dispatch.md)
> (turns are refcounted; `MidTurn` decides mid-turn arrival),
> [`per-message-events.md`](per-message-events.md) (every event carries the `message_id` that
> caused it) and [`mid-turn-steering.md`](mid-turn-steering.md) (the adapters render harness
> text into tool results).

## The problem

A user watching an agent sometimes needs it to stop now: the model is taking too long, or it
is plainly going off the rails with one tool call after another. Today the only way to stop
it is to close or terminate the workflow, which loses the session.

An interrupt should end the open turn as fast as it safely can and leave the agent idle and
open, so the user decides what happens next. Concretely:

- the model call in flight is cancelled before it finishes;
- tool calls that have not started never run;
- tool calls that are running are cancelled, and their authors get to clean up;
- the model is told which tools ran and which did not, and can answer the interruption
  ("sorry — what did you want me to do?");
- nothing can extend the interrupted turn: no new tool runs, and no message joins it.

## What the harness owns, and what it does not

The model loop belongs to the agent's author. An agent calls `Runner.run_streamed`, Pydantic
AI's `agent.run`, or its own Gemini loop, and it keeps its own conversation history
(`self._conversation = result.to_input_list()`). The harness cannot decide whether another
model call happens, which tools that call offers, or what the loop does with the response.

So the harness enforces exactly one thing about the model's behavior: **after an interrupt,
`run_tool` refuses every call**, with a message telling the model why and what to do. A model
that ignores the first refusal gets the same one for every later call, so it stops after a
few. Everything else the harness does acts on work it owns:

| The harness, for every agent, with no author code | The author's loop |
| --- | --- |
| The `interrupt_turn` update, and rejecting joins while the turn drains | Whether to make a reply call after the interrupt, and with which tools |
| `run_tool` refusing every call after the interrupt | What to do when its in-flight model call is cancelled |
| Cancelling the model activity in flight when the interrupt arrives | Saving the turn's partial history |
| Cancelling in-flight activity tools, and reporting what each stopped tool did | Stopping once the model has got the point |
| Dropping queued steering, denying pending approvals, settling pending callbacks | |

## Surface

### The `interrupt_turn` update

A protocol-level update on every agent workflow, beside `tool_approval`,
`provide_callback_result` and `close`. It is not an `@agent.accepts` handler: an interrupt
must work on any agent without its author writing anything.

```python
class InterruptTurnRequest(BaseModel):
    turn_id: str  # the turn the caller means to interrupt


class InterruptTurnAck(BaseModel):
    turn_id: str
    already_interrupted: bool
```

The validator rejects:

- `NoTurnToInterrupt` when no turn is open;
- `StaleInterrupt` when `turn_id` is not the open turn. Without the id, a click that lands
  just after one turn ended and the next queued turn opened would interrupt the wrong turn.

The handler is synchronous (like `_handle_send_agent_message`) and returns at once. The caller
learns the turn has drained from `turn_end`, as it does for any turn.

Interrupting a turn that is already interrupted succeeds with `already_interrupted=True`. The
turn-level effects below happen only once, but every call cancels whatever model activity is in
flight at that moment. A second click therefore stops a reply call the user does not want to
wait for, without any new mechanism.

`AgentClient.interrupt_turn(turn_id)` wraps the update, the server exposes it, and the packaged
UI shows a stop control while a turn is open.

### What an interrupt does

At the moment the update is handled, in one workflow task:

1. The runner records the turn as interrupted and publishes `turn_interrupted`.
2. Queued steering is dropped and `steering_dropped` is published. It was written for work the
   user has just stopped.
3. Pending approvals are denied with reason "turn interrupted", the same way `close` denies
   them. Running auto mode evaluators are cancelled with `_cancel_and_settle`.
4. Pending callbacks are settled as interrupted. The validator then rejects a late
   `provide_callback_result` for them, using the entry status it already checks.
5. The in-flight model activity, if any, is cancelled (see Model calls).
6. In-flight tool activities are cancelled, unless they opted out (see Tool calls).

### Admission while the turn drains

- A `MidTurn.ACCEPT` message is rejected in the validator with a typed `TurnInterrupted`
  error instead of joining. Joining is the one way a message extends the open turn.
- `MidTurn.ENQUEUE` behaves as it always does: the message queues and opens its own turn after
  this one ends. It does not extend the interrupted turn, and a message sent after an interrupt
  is usually the user saying what to do next. Messages queued before the interrupt stay queued
  as well.
- `MidTurn.REJECT` is unchanged.
- `steer()` raises `TurnInterrupted`, since there is no turn left to steer.

## Tool calls

### Calls that have not started

`run_tool` checks the interrupt before it dispatches anything, including the approval gate.
An interrupted call never runs, and the adapter returns this to the model as the tool's result:

```
Not run: the user interrupted this turn. Tool calls are disabled until you end this turn.
Reply to the user and stop; they will tell you what to do next, and your tools will be
available again in the next turn.
```

`run_tool` raises `ToolCallInterrupted` (a `TurnInterrupted`) and the adapters turn it into
that text, the way they already turn a failure into `Tool 'x' failed: …`. Raising rather than
returning the string keeps `run_tool`'s typed return values intact, and lets a Code Mode script
or a hand-written loop tell an interrupted call from a tool that returned text.

### Activity tools in flight: cancel by default

Agents should be highly interactive by default, so an interrupt cancels running activity tools
unless the tool opts out.

```python
@agent.activity_tool_defn  # on_interrupt=OnInterrupt.CANCEL is the default
async def run_migration(plan: Plan) -> MigrationReport:
    try:
        ...
    except asyncio.CancelledError:
        await rollback(plan)
        raise  # or return a MigrationReport describing what was done before stopping


@agent.activity_tool_defn(on_interrupt=OnInterrupt.FINISH)
async def charge_card(order_id: str) -> Receipt: ...
```

Temporal delivers an activity's cancellation only in the response to a heartbeat. Today
`activity_tool_defn` neither sets a heartbeat timeout nor heartbeats, so a cancel would never
reach the tool: under `TRY_CANCEL` the workflow would stop waiting while the tool carried on and
its side effects still happened. Telling the model "cancelled" would then be false.

**Every activity tool heartbeats**, whatever its `on_interrupt`. The decorator:

- sets a heartbeat timeout (default 10s) when `activity_config` has none, and keeps the
  author's when it has one;
- runs a heartbeat task inside the activity body for the whole call. Authors never call
  `heartbeat()` themselves.

Heartbeating does two things:

- **Crashed workers are noticed quickly.** Today a tool whose worker dies is not noticed until
  `start_to_close_timeout` expires (30s by default, and often set to minutes for slow tools).
  With a heartbeat timeout, Temporal times the attempt out within that timeout and retries it
  under the tool's retry policy, so the turn never sits waiting on a worker that is gone. That
  holds for every tool, including one that will not be cancelled.
- **A cancel can reach the tool.** The Python SDK throttles heartbeats to 0.8 × the heartbeat
  timeout, so a cancel arrives within about 8 seconds at the default timeout.

The heartbeat task proves the worker process is alive, not that the tool is making progress: a
tool stuck on a call that never returns keeps heartbeating. `start_to_close_timeout` still
bounds that case, as it does today.

**This changes behavior for existing tools that block the event loop.** The heartbeat task
shares the activity's event loop. An `async` tool that does blocking work, such as CPU-heavy
code or a synchronous library call, keeps that task from running. Today such a tool succeeds
as long as it finishes within `start_to_close_timeout`. With a heartbeat timeout, it times out
after the heartbeat timeout and is retried, and it can fail every attempt the same way. The fix
on the author's side is the usual one for asyncio: move the blocking work to a thread with
`asyncio.to_thread`. A tool that cannot be changed can set a longer `heartbeat_timeout` in its
`activity_config`. The release notes for the phase that turns heartbeating on must say this, and
the harness docs on writing tools should say it next to `activity_tool_defn`.

`on_interrupt` decides only whether the workflow sends that cancel. `OnInterrupt.CANCEL`
requests it on interrupt and uses `WAIT_CANCELLATION_COMPLETED`, so the workflow moves on only
once the activity has really stopped.

Activity tools are always `async`, so the cancel arrives as `asyncio.CancelledError` at
whatever `await` the tool is on. Cleanup is an ordinary `except` or `finally`, as it is for any
cancelled asyncio task. The harness reports what the tool did:

| The tool… | Reported as |
| --- | --- |
| re-raises `CancelledError` | cancelled while running |
| returns a value | stopped early, with that value |
| raises something else | failed, as today |

`OnInterrupt.FINISH` sends no cancel. The tool keeps heartbeating, so a crashed worker is still
noticed within the heartbeat timeout, but no heartbeat response ever reports a cancellation, so
no `CancelledError` is raised inside it. The tool runs to completion, its real output is
reported as completed after the interrupt, and the turn cannot end until it does. Use it for a
tool whose work must not be cut off part-way and that has no meaningful cleanup.

For a `CANCEL` tool, the decorator rejects an `activity_config` whose `cancellation_type` is not
`WAIT_CANCELLATION_COMPLETED`, at definition time.

### Other kinds of in-flight call

| In-flight kind | On interrupt |
| --- | --- |
| Workflow tool (`tool_defn`) running inline | Runs to its next `await`; whatever it awaits is covered by the rows below |
| Waiting on approval | Denied; reported as not run |
| Callback tool | Settled as interrupted; reported as outcome unknown. The harness cannot stop work the client is doing; a client should abort it when it sees the event |
| Subagent call | Cascades: the parent sends the child `interrupt_turn` for the child's open turn and waits for it to drain. The child's reply becomes the tool result |
| Code Mode `run` | Host calls not yet started raise `ToolCallInterrupted` inside the script, and the driver stops stepping. In-flight host calls follow the rows above. The `run` result lists the host calls that finished |
| MCP call through the harness wrapper | Goes through `run_tool`, so it follows its transport: an activity follows `on_interrupt`, an inline call runs to its next `await` |

### What the model is told

Every outermost tool result that reaches the model after the interrupt states what happened
to that call: not run, cancelled while running, stopped early (with what it returned), or
completed after the interrupt (with its output). Each one ends with the same instructions as
the refusal message. The adapters render these, as they render steering, because they are the
code that knows the SDK's tool-result shape. These results are in the author's history like any
other tool result, so the model still knows in later turns what did and did not run.

## Model calls

The harness owns the model activities (`invoke_model_activity` and its streaming variant for
OpenAI Agents, the Pydantic AI model-request activity, the Gemini activities), so it cancels
them itself.

- **Heartbeats always on.** Model activities get a default heartbeat timeout of 5s, and
  `auto_heartbeater` runs whenever one is set, so a cancel arrives within about 4 seconds.
  Cancellation becomes `WAIT_CANCELLATION_COMPLETED`: the workflow continues only after the
  activity has stopped publishing, so no stray deltas from a cancelled call reach the stream
  after it.
- **Inside the activity.** On `CancelledError` the activity leaves the provider's stream, which
  closes the HTTP connection. It then publishes `model_interaction_ended` with
  `interrupted=True` and the usage it has seen, flushes its publisher, and re-raises.
- **Saving tokens where we can.** For streaming calls, closing the connection is how providers
  stop generating. Tokens already produced are still billed. A non-streaming call usually
  finishes generating on the provider's side and is billed in full, so cancelling it saves time,
  not tokens. Where a provider has an explicit cancel (OpenAI Responses background mode), the
  activity calls it.
- **What the SDK sees.** The workflow-side seam raises `ModelCallInterrupted` (a
  `TurnInterrupted`) into the SDK run. It does not invent a response: what happens next is the
  author's loop's decision.
- **Only the call in flight is cancelled.** Model calls that start after the interrupt run
  normally, so the model can answer the interruption.

### What the author's loop does

An author who wants the model to answer an interruption that landed mid-model-call catches
`ModelCallInterrupted`, saves what the run had produced so far, and makes one more call with the
interrupt explained, typically as a user item and often with tools turned off. An author who
does not want that lets the turn end.

The partial history needs care. When the exception escapes `Runner.run_streamed`, the line that
saves `result.to_input_list()` never runs, and the tool calls and results the turn made before
the cancelled model call are lost from the agent's history. The examples show catching the
exception and keeping the partial run. OpenAI Agents attaches run data to the exceptions it
raises; Pydantic AI exposes the messages so far through `agent.iter`.

The runner exposes `runner.interruption` (`None`, or the turn's interrupt state with a
`render()` for a hand-written loop's own input) so a loop can check it without catching
anything.

A handler that lets a `TurnInterrupted` escape ends with `message_handler_error(interrupted=True)`,
so a client can show it as interrupted rather than failed.

## Model interaction ids

Events published during a model call carry the id of that call, the same way every event
carries the `message_id` of the message that caused it.

- The workflow-side seam mints `model_interaction_id` with `workflow.uuid4()` before it
  schedules the model activity. It rides into the activity on `TurnStreamContext`, beside
  `message_id`, and `TurnEventPublisher` stamps it on a new `AgentEvent.model_interaction_id`
  envelope field. It is `None` for every event not produced by a model call.
- Minting it in the workflow keeps it stable across activity retries. A later change can stamp
  `activity.info().attempt` next to it, so each attempt of an interaction is distinguishable;
  that is out of scope here.
- `interrupt_turn` uses it to name the model call it cancelled, and a client uses it to group
  deltas by interaction instead of by stream order.

## Exceptions

```python
class TurnInterrupted(Exception): ...              # the open turn was interrupted
class ToolCallInterrupted(TurnInterrupted): ...    # run_tool refused or stopped a call
class ModelCallInterrupted(TurnInterrupted): ...   # the in-flight model call was cancelled
```

One `except TurnInterrupted` covers every way an interrupt reaches author code. The
`send_agent_message` validator's rejection uses the same name as its `ApplicationError` type.

## Events

One new event, and no new terminal events. Everything an interrupt stops already has a terminal
event; each of those gains an `interrupted: bool` (default `False`) instead.

| Event | Published when | Body | Envelope `message_id` |
| --- | --- | --- | --- |
| `turn_interrupted` | `interrupt_turn` first applies to a turn | — | `None` |

**Why `turn_interrupted` is needed.** It does not stand in for anything already published:
`turn_started` has been published before anyone can interrupt the turn. It marks the start of
the drain, which can be long: a `FINISH` tool running to completion, a cancel waiting on the
heartbeat throttle, a subagent draining its own turn, the model's reply call. In that window a
client needs to show that the turn is being interrupted, stop offering the stop control again,
and stop offering mid-turn input, which would now be rejected. `turn_end.interrupted` alone would
tell it only once the drain is over. It also places every later event relative to the
interrupt, which is how a client tells a tool that completed after the interrupt from one that
completed before it. A client that attaches during the drain reads the same fact from the
`agent_status` query.

**`interrupted` on existing events:**

| Event | `interrupted=True` when |
| --- | --- |
| `turn_end` | the turn that ended was interrupted. Repeats what `turn_interrupted` said, so a consumer that only watches turn ends need not fold the stream |
| `model_interaction_ended` | the model call was cancelled before it finished |
| `tool_error` | the call was refused before it started, or cancelled while running and re-raised |
| `tool_end` | the tool returned a value after its cancel arrived (it stopped early) |
| `callback_resolved` | the pending callback was settled by the interrupt, so its outcome on the client is unknown |
| `message_handler_error` | the handler let a `TurnInterrupted` escape: an expected outcome, not a failure |

A tool that finishes normally after the interrupt (a `FINISH` tool, or a `CANCEL` tool whose
cancel had not arrived yet) ends with an ordinary `tool_end`: it was not interrupted.

**Where the tool events are published.** A cancelled activity tool publishes
`tool_error(interrupted=True)` from inside the activity, on its `CancelledError` path, so it is
ordered before the activity ends. That path is new: the activity body publishes `tool_error`
only from `except Exception`, and `CancelledError` is not an `Exception`, so today a cancelled
tool would publish no terminal event at all. A refused call publishes it from `run_tool`, which
also gives a refused call a terminal event: an ungated call that never runs would otherwise end
on `tool_requested`.

And one new envelope field, `AgentEvent.model_interaction_id: str | None` (see Model
interaction ids).

## Behavior in the cases that need care

- **Parallel tool calls.** Each call is handled on its own: those not started are refused, the
  running ones are cancelled or allowed to finish. All their results reach the model in the same
  request.
- **Concurrent participants.** The interrupt is for the turn, not for one participant. Every
  participant's tool calls are refused and its model call cancelled. Handlers with no model
  loop are not touched and finish normally.
- **Handlers are never cancelled.** Cancelling the participant task would discard the handler's
  reply and unwind the SDK run partway through. A handler that waits on its own timer or
  condition can also wait on `runner.interruption`.
- **`close` during the drain.** Close behaves as it does today: it waits for the turn to end.
  The interrupt only makes that happen sooner.
- **Subagents.** Interrupting a parent interrupts children only through in-flight subagent
  calls. A child turn that a parent call is not waiting on is left alone.
- **An interrupt during the reply call.** The turn is already interrupted, so a second
  `interrupt_turn` only cancels that model call (see `interrupt_turn`).

## Alternatives considered

- **The harness forces a closing model call** (send it with `tool_choice="none"`, strip tool
  calls from its response, refuse any model call after it). Rejected: those are decisions about
  the author's loop, which the harness does not own.
- **Abandon running tools** (`TRY_CANCEL` without waiting). Rejected: the tool keeps running
  and its side effects land, while the model is told it was cancelled. Its `tool_end` would also
  arrive after the turn moved on.
- **Let running tools finish by default.** Rejected in favor of cancelling by default, so
  agents stay interactive. A tool opts out with `OnInterrupt.FINISH`.
- **Return a made-up model response when a model call is cancelled.** Rejected: it would end
  the author's run with text nobody wrote. An exception leaves the choice to the loop.
- **Reject `ENQUEUE` messages until the turn drains.** Rejected: a queued message does not extend
  the interrupted turn.
- **Cancel the participant tasks.** Rejected; see Behavior in the cases that need care.

## To verify before implementing

Each is settled in the phase that first depends on it (see Delivery plan).

- What the workflow receives when an activity completes successfully after its cancellation
  was requested, under `WAIT_CANCELLATION_COMPLETED`. The stopped-early outcome
  (`tool_end(interrupted=True)`) assumes the result comes through. Phase 2.
- How each provider bills after a streaming disconnect, and which offer an explicit cancel.
  Phase 5, by manual check.
- That `ModelCallInterrupted` reaches the author unwrapped through `Runner.run_streamed` and
  Pydantic AI's run, and how each exposes the partial run when it does. Phase 5.
- That the Pydantic AI integration lets the harness cancel the model-request activity from the
  workflow side; its durability layer schedules that activity. Phase 5c.

## Delivery plan

The whole design is roughly twice the size of mid-turn steering (#283: 76 files, about 3,000
lines), around 6,000 to 7,000 lines across both SDKs, about half of them tests and golden data. It ships as six
PRs. Each one is complete in both SDKs, has its own tests, and leaves the harness working; later
phases only make the interrupt reach further. Phases 1 and 2 are useful even if the interrupt
itself never ships.

Every phase follows the repository's test rules: time-skipping environments, every wait on a
workflow bounded, no slow tests. Checks against real providers (what a provider bills after a
disconnect) are manual runs, listed for the user to do, not automated tests.

### Phase 1: model interaction ids

- **Ships:** `model_interaction_id` minted by each model seam, carried on `TurnStreamContext`,
  and stamped on `AgentEvent.model_interaction_id`. All three integrations (OpenAI Agents,
  Pydantic AI, Gemini) in both SDKs where they exist. The client projection groups reply deltas
  by interaction.
- **Tests:** every event of one streamed model call carries the same id; two calls in one turn
  carry different ids; a model activity that fails its first attempt and succeeds on retry keeps
  one id; events not produced by a model call carry `None`; the conformance normalizer numbers
  the ids, and existing goldens are updated.
- **Size:** about 400 lines.
- **Behavior change:** none beyond the new envelope field.

### Phase 2: heartbeating activity tools

- **Ships:** the default heartbeat timeout and the heartbeat task in `activity_tool_defn` for
  every tool; `on_interrupt` with `CANCEL` and `FINISH`, including the definition-time
  `cancellation_type` check; `tool_error(interrupted=True)` from the activity body's
  `CancelledError` path; `tool_end(interrupted=True)` for a tool that returns after its cancel.
  Nothing sends the cancel yet except a workflow that cancels the activity itself.
- **Tests:** the scheduled activity carries the default heartbeat timeout, and an author's own
  timeout is kept; a test workflow that cancels a running `CANCEL` tool sees the author's
  `except CancelledError` run and `tool_error(interrupted=True)` published before the activity
  ends; a tool that returns a value after the cancel produces `tool_end(interrupted=True)`.
- **Answers a to-verify item:** what the workflow receives when an activity completes after its
  cancel was requested. If the result does not come through, the stopped-early outcome is
  dropped from the design before phase 3.
- **Size:** about 500 lines.
- **Behavior change:** the blocking-tool caveat above. The release notes say so.

### Phase 3: the interrupt in the workflow

- **Ships:** `interrupt_turn` with its validator and ack; interrupt state on `_WorkflowStatus`;
  `turn_interrupted` and `turn_end.interrupted`; rejecting `MidTurn.ACCEPT` joins and `steer()`;
  the `run_tool` refusal and the adapters rendering it and the stop statuses into tool results;
  denying pending approvals, settling pending callbacks (`callback_resolved(interrupted=True)`)
  and dropping steering; cancelling in-flight `CANCEL` tools; `TurnInterrupted` and
  `ToolCallInterrupted`; `message_handler_error(interrupted=True)`; `runner.interruption`;
  `AgentClient.interrupt_turn` and the server route.
- **Tests:** a conformance golden for every point at which a turn can be interrupted (below),
  played against both SDKs' workers; adapter tests for the refusal text and stop statuses in
  each adapter's tool-result shape; a unit test for an interrupt handled in the same workflow
  task as the turn's last participant leaving, which a scenario cannot place deterministically.
- **Size:** about 3,000 lines: about 1,700 of code and tests, about 400 of conformance harness
  additions, and about 1,500 of scenario and golden data.

#### Goldens: every interrupt point

Each row is a conformance scenario with its events golden (and a sends golden where the driver
expects a rejection). The goldens are the specification of interrupt behavior: a later change
that alters any of them has changed the behavior and must say so.

**Conformance harness additions, in both SDKs' scripted agents and the driver:**

- **New driver actions:** an `interrupt` action, naming the turn of a message the scenario sent
  or an explicit stale `turn_id`; the driver records the ack, or the rejection's error type, in
  the sends golden.
- **A `parallel` script step,** running several tool steps at once, as one model step's calls
  are.
- **Hold tools,** activity tools that stay running until the interrupt reaches them, so a
  scenario can interrupt a tool that is deterministically in flight:
  - `hold_cancel` (`CANCEL`) re-raises the cancel;
  - `hold_return` (`CANCEL`) returns a value when cancelled;
  - `hold_finish` (`FINISH`) is never cancelled. It polls the agent's `agent_status` query until
    the turn shows as interrupted, then returns. That keeps it deterministic in every SDK's
    worker without a test-only channel into the activity.
- **An auto mode evaluator** in the scripted agent that stays undecided until cancelled.
- **A script option for interrupts:** whether the scripted loop renders `ToolCallInterrupted`
  as an adapter does and continues (the default), or lets it escape the handler.
- **Ignoring refusals:** script steps after the interrupt point keep running. That stands in for
  a model that ignores the refusal and keeps calling tools.

**Validation and admission:**

| Scenario | Pins |
| --- | --- |
| Interrupt with no open turn | `NoTurnToInterrupt`; no events |
| Interrupt naming a turn that has ended while the next queued turn runs | `StaleInterrupt`; the running turn is unaffected |
| Interrupt twice | second ack has `already_interrupted=True`; one `turn_interrupted` |
| `ACCEPT` message during the drain | `TurnInterrupted` rejection; no `message_accepted` |
| `REJECT` message during the drain | `MidTurnRejected`, as before |
| `ENQUEUE` message during the drain | queued; opens its own turn after `turn_end(interrupted=True)`; its tools run normally, so the interrupt does not outlive its turn |
| `close` during the drain | the turn drains, then `agent_closed` |

**Tool calls:**

| Scenario | Pins |
| --- | --- |
| `hold_cancel` running | `tool_error(interrupted=True)` from the activity before the workflow continues |
| `hold_return` running | `tool_end(interrupted=True)` with the returned value (if phase 2 confirms the result comes through) |
| `hold_finish` running | plain `tool_end` after `turn_interrupted`; `turn_end` waits for it |
| Gated call waiting on approval | approval denied with reason "turn interrupted"; the call never starts |
| Auto mode evaluator deciding a gated call | the evaluation cancelled; the call never starts |
| Callback pending | `callback_resolved(interrupted=True)`; a later `answer_callback` rejected |
| Calls after the interrupt, one of each kind (workflow tool, activity tool, gated tool, callback tool) | each ends with `tool_error(interrupted=True)` and the refusal text, with no `tool_start`, `tool_approval_requested` or `callback_requested` |
| One `parallel` step with `hold_cancel`, `hold_finish`, a gated call and a not-yet-started call | each call's own outcome from the rows above, in one step |
| Handler lets `ToolCallInterrupted` escape | `message_handler_error(interrupted=True)`, then `turn_end(interrupted=True)` |

**The rest of the turn:**

| Scenario | Pins |
| --- | --- |
| Steering queued when the interrupt arrives | `steering_dropped` right after `turn_interrupted`; nothing claimed afterwards |
| A model step after refused calls | the reply publishes normally; the turn then ends with `turn_end(interrupted=True)` |
| Two participants (opener and a joined `ACCEPT` handler), each running a hold tool | both participants' tools follow the rows above; the turn ends when the last one leaves |
| Subagent call in flight | phase 3 behavior: the parent waits for the child's turn to end normally (phase 6 changes this golden) |

**Points added by later phases.** Phase 5 adds a model step that runs as a holding model
activity, and a golden for an interrupt during it, per integration. Phase 6 adds a Code Mode
golden and updates the subagent golden to the cascade. A phase that changes the behavior at an
existing point updates that point's golden in the same PR, so the diff shows the change.
- **Still not covered:** an in-flight model call finishes before the turn drains, so the model
  sees the refusals only after it. Subagent calls wait for the child's turn to end normally.
  Code Mode's unstarted host calls are already refused, because they go through `run_tool`; the
  script keeps running until it ends.

### Phase 4: the interrupt in clients and the UI

- **Ships:** the client projection folding `turn_interrupted` and the `interrupted` fields; an
  interrupt action and interrupted state in the React and Svelte bindings; the stop control in
  the packaged UI, shown while a turn is open and replaced by an "interrupting" state during the
  drain; interrupted markers on tool cards, model interactions and handler results.
- **Tests:** projection tests over the phase 3 goldens; binding tests for the action and state;
  UI tests for the control and the markers.
- **Size:** about 600 lines.

### Phase 5: cancelling model calls

One PR per integration, so the Pydantic AI unknown does not hold up the others:

- **5a, OpenAI Agents (Python and TypeScript):** heartbeats on by default for model activities
  and `WAIT_CANCELLATION_COMPLETED`; the activity leaving the provider stream, publishing
  `model_interaction_ended(interrupted=True)` and re-raising; the stub cancelling on interrupt
  and raising `ModelCallInterrupted`; a second `interrupt_turn` cancelling the reply call; the
  examples catching the exception and keeping the partial history.
- **5b, Gemini (Python and TypeScript):** the same, in the Gemini activities and client.
- **5c, Pydantic AI:** starts with a spike on whether the harness can cancel the model-request
  activity from the workflow side. If it cannot without owning more of Pydantic AI's durability
  layer, this PR is rescoped or deferred, and Pydantic AI agents keep phase 3 behavior.
- **Tests, per integration:** a fake streaming model interrupted mid-stream;
  `model_interaction_ended(interrupted=True)` published, and no deltas with that interaction id
  after it; `ModelCallInterrupted` reaching the handler unwrapped through the SDK's run; a
  second interrupt cancelling the next model call; a conformance scenario interrupting during a
  model call.
- **Manual check (for the user to run):** against each real provider, that interrupting a long
  streaming call stops generation, and what is billed.
- **Size:** about 1,200 lines across the three.

### Phase 6: subagents and Code Mode

- **Ships:** an in-flight subagent call interrupting the child's open turn and returning the
  child's reply as the tool result; the Code Mode driver stopping once the turn is interrupted,
  with the `run` result listing the host calls that finished.
- **Tests:** a parent interrupted while a child turn runs, the child's tool refused and its
  reply returned to the parent; a Code Mode script interrupted between host calls, with the
  later calls refused and the `run` result listing the earlier ones.
- **Size:** about 600 lines.

## TypeScript

Same design, same names in camelCase, at the mirrored paths. The activity-side cancellation is
`Context.current().cancellationSignal`, an `AbortSignal` a tool can pass straight to `fetch`;
the decorator's heartbeat and `onInterrupt` behave as in Python.

## Implementation touch points

- `harness/agent_workflow.py`: interrupt state on `_WorkflowStatus`; the `interrupt_turn` update
  and validator; the `ACCEPT` rejection; the refusal in `run_tool`; a default heartbeat timeout
  and a heartbeat task in `activity_tool_defn`'s activity body for every tool, and
  `on_interrupt` deciding whether the interrupt cancels it; settling approvals, callbacks
  and steering; `runner.interruption`; `interrupted` on `message_handler_error` and `turn_end`
  in `_run_participant`; publishing `tool_error(interrupted=True)` on the activity body's
  `CancelledError` path.
- `harness/stream_context.py`: `model_interaction_id` on `TurnStreamContext`.
- `harness/agent_protocol/events.py` and `events.ts`: the new events and fields; regenerate
  `contract/protocol.schema.json`.
- The model seams and activities: `ai_sdks/openai_agents` (`_temporal_model_stub.py`,
  `_invoke_model_activity.py`, `_model_parameters.py`), `ai_sdks/pydantic_ai_harness.py`,
  `ai_sdks/google_genai_plugin`, and the stream observers in `integration_helpers`.
- The adapters (`openai_agents_harness.py`, `pydantic_ai_harness.py`, the TS adapter): render
  `ToolCallInterrupted` and the in-flight statuses into tool results.
- `harness/subagent_toolset.py` and `subagent_activities.py`: cascade to the child.
- `harness/code_mode`: stop stepping once interrupted.
- `agent_client.py`, the server and the packaged UI: `interrupt_turn` and the stop control.
- Examples: catching `ModelCallInterrupted` and keeping the partial run, in each SDK.
- A conformance scenario with goldens for an interrupt during a model call and one during tool
  calls.
