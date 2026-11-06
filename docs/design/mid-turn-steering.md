# Mid-turn steering: a message that rides the next tool result

> Status: **accepted, and implemented** in both SDKs, the client projection, the conformance
> suite and the packaged UI. **What changed on the way in** records where the implementation
> differs from the plan below. Builds on
> [`unified-message-dispatch.md`](unified-message-dispatch.md) (a `MidTurn.ACCEPT` message
> joins the open turn and runs concurrently with it) and
> [`per-message-events.md`](per-message-events.md) (every event carries the `message_id` of
> the message that caused it).

## The problem

A user watching an agent work wants to correct it without stopping it: "focus on the auth
module first", "skip the tests for now". The dispatch model already admits such a message —
a handler declared `@agent.accepts(mid_turn=MidTurn.ACCEPT)` joins the open turn and can tell
it joined via `MessageContext.joined_turn`. What it cannot do is get the text in front of the
model. The `MessageContext` docstring sketches the intent
(`self._steering.append(msg.text)  # drained by the running model loop`), but nothing drains
it, and an author has no way to write the draining half: the model loop belongs to whatever
AI SDK the agent uses, and in SDKs that call tools automatically, no author code ever
constructs the request that follows a tool result.

The harness should make steering a one-line call from a mid-turn handler, and deliver it to
the model at the next opportunity without the author touching the model loop.

## Where the steering goes

The next request to the model after a tool call is that tool's result. So steering is
delivered **appended to the end of the next tool result that resolves**, as a fixed XML
block the model can recognize as not being the tool's output.

Two constraints decide which tool result.

**Code Mode's host calls must never carry it.** A Code Mode tool (`code_mode_tool`) runs a
script whose host calls are real harness tools, dispatched through the same
`AgentWorkflowRunner.run_tool` as the model's own calls (`code_mode/driver.py`). Their results
are read by the script, not the model, and the script interprets them programmatically —
a steering block appended to one corrupts a value the script indexes into. Only the outer
`run` call's result reaches the model.

**The harness does not know where the model call happens.** An agent may use OpenAI
Agents, Pydantic AI, a hand-written Gemini loop, or several model calls inside one handler.
The harness cannot hook "the next model request" directly; it can only hook the points it
owns, which are `run_tool` and the per-SDK adapters that wrap it.

### The rule: only an outermost tool call carries steering

`run_tool` parks the in-flight call's id in the ambient `_CURRENT_TOOL_ID` for the duration
of the call (`toolCallStore` in TypeScript). A tool call that starts while that is already set
is running *inside* another tool — a Code Mode host call, an `fs_*` file operation, or any
tool that composes other tools. So:

> Steering may be claimed only when no tool call is in flight in the current context.

This is not a Code Mode special case. It holds for every tool that calls other tools,
including ones not written yet. Code Mode's concurrent host calls are covered too: the driver
runs them under `asyncio.gather`, which copies the context, so each one still sees the outer
`run` call's id.

### Why the adapters attach it, not `run_tool`

`run_tool` returns the tool's typed value — a string, a dict, a Pydantic model, an MCP
`CallToolResult`. That value becomes model input later, in code `run_tool` does not own:

| Path | Where the result becomes model input |
| --- | --- |
| OpenAI Agents | `as_openai_agent_tools` → `_stringify_tool_result(result)` |
| OpenAI Agents, MCP | the `call_tool` wrapper returns the `CallToolResult` |
| Pydantic AI | `as_pydantic_ai_tool`'s `on_invoke_tool` returns the value; Pydantic AI serializes it |
| Hand-written loops (the Gemini examples) | the author builds the `function_result` item |

Appending a string inside `run_tool` would corrupt every non-string result. So the work is
split: the **runner** owns the queue and the outermost-only rule; the **adapter** (or the
author of a hand-written loop) owns rendering, because it is the code that knows the shape
the SDK sends to the model. Every SDK integration already wraps each tool in a harness-owned
function between `run_tool` and the SDK, so SDK users get steering with no code of their own.

## Proposed surface

Two methods on `AgentWorkflowRunner`.

```python
def steer(self, text: str) -> str:
    """Queue ``text`` to reach the model with the next outermost tool result; returns its
    ``steering_id``."""

def claim_steering(self, *, tool_id: str | None = None) -> ClaimedSteering | None:
    """Take everything queued, for attaching to content about to go to the model."""
```

```python
@dataclass(frozen=True)
class SteeringMessage:
    steering_id: str
    message_id: str
    text: str


@dataclass(frozen=True)
class ClaimedSteering:
    messages: tuple[SteeringMessage, ...]

    def render(self) -> str:
        """The XML block to append to model input (see Format)."""
```

### `steer(text)`

- Appends to a queue held in workflow state, so it is durable and replays deterministically.
  No in-memory worker state.
- Returns immediately. A steering handler does not wait for delivery: it queues and returns,
  and its own `message_handler_end` follows at once. Returning does not end the turn — the
  turn closes only when its last participant finishes, and the participant running the model
  loop is still in it.
- Attributes the steering to the ambient `_CURRENT_MESSAGE_ID`, the message whose handler is
  calling it. Raises if there is none (outside a message handler).
- Mints a `steering_id` for this one piece of steering, publishes it on `steering_enqueued`,
  and returns it. The claim or drop that settles it names it by that id.

The intended handler:

```python
@agent.accepts(mid_turn=MidTurn.ACCEPT)
async def user_text(self, msg: UserText, ctx: Injected[MessageContext]) -> TextReply:
    if ctx.joined_turn:
        self._runner.steer(msg.text)
        return TextReply(text="")
    return await self._prompt(msg.text)
```

The harness does **not** ship a built-in steering handler. Authors write one like the above;
a built-in can come later if every agent ends up writing the same thing.

### `claim_steering(tool_id=...)`

- If a tool call is in flight in the current context, returns `None` and leaves the queue
  untouched. The steering waits for the outermost result.
- Otherwise removes every queued message and returns them in arrival order, or `None` if the
  queue is empty. Removal is what delivery means: a claimed message is about to be attached,
  and leaving it queued would attach it again to the next result.
- Publishes `steering_claimed` when it returns messages.
- `tool_id` is the id of the tool call whose result will carry the block. Pass `None` when
  the steering goes in as something other than a tool result (a user item in a hand-written
  loop, or a follow-up prompt).
- Synchronous by design. With no `await` between checking and clearing the queue, two
  concurrent claimants on the single-threaded workflow loop cannot both take the same message.
  It must stay synchronous.

The adapters call it right after `run_tool` returns — on the success path and on the path
that turns an exception into a model-visible `"Tool 'x' failed: …"` result, since that also
reaches the model.

```python
result = await runner.run_tool(call_id, tool, **kwargs)
text = _stringify_tool_result(result)
if steering := runner.claim_steering(tool_id=call_id):
    text = f"{text}\n\n{steering.render()}"
return text
```

Per adapter:

- **OpenAI Agents:** append to the already-stringified output.
- **OpenAI Agents MCP wrapper:** append a `TextContent` block holding the render to the
  `CallToolResult`'s content.
- **Pydantic AI:** if the result is a string, append. Otherwise serialize it first with the
  same JSON serialization Pydantic AI applies to non-string returns
  (`ToolReturnPart.model_response_str`), then append. Calls that carry no steering are
  unchanged. One visible difference on the carrying call only: for Gemini models Pydantic AI
  wraps a non-dict return as `{"return_value": ...}`, so a dict result arrives wrapped as a
  string rather than as the dict.
- **Hand-written loops:** the author calls `claim_steering(tool_id=call.id)` when building
  each `function_result` and appends the render to its result. The `code_mode_memory`
  example shows this.

### Format

```xml
<steering_messages>
The user sent the following while you were working. Take them into account before continuing.
<message>focus on the auth module first</message>
<message>actually skip the tests for now</message>
</steering_messages>
```

- One block per claim. Messages in arrival order, one `<message>` each, no attributes.
- The explanatory line is fixed and inside the block. The harness does not own the system
  prompt, so the block must explain itself; without it the model sees an unfamiliar tag at
  the end of a tool result and has to guess whether the tool produced it.
- Message text is XML-escaped (`&`, `<`, `>`), so text containing `</message>` cannot break
  the structure.
- No message ids. The model has nothing to do with them; correlation lives in events.

## Events

Three new event types, alongside the `message_*` family.

| Event | Published when | Body | Envelope `message_id` |
| --- | --- | --- | --- |
| `steering_enqueued` | `steer(text)` is called | `steering_id`, `text` | the steering message |
| `steering_claimed` | `claim_steering` returns messages | `steering_ids`, `tool_id: str \| None` | the claiming participant (the one running the model loop) |
| `steering_dropped` | the turn ends with steering still queued | `steering_ids` | `None` |

**Why each steering has its own id.** A claim or drop could be read as "everything enqueued
since the last one", but then a consumer relates them by stream order and timestamps, and a
steer enqueued at the moment of a claim is ambiguous. Naming each piece of steering on
`steering_enqueued`, and listing exactly those ids on the event that settles it, makes the
relation explicit. The id is not the `message_id`: one handler can steer more than once, and
each piece of steering is settled on its own.

**Why `steering_enqueued` is separate from `message_accepted`.** `message_accepted` says the
handler was admitted. The handler decides whether to steer — typically only when
`ctx.joined_turn` — so admission does not imply steering.

**Why "claimed", not "delivered".** The harness can observe that model-input code took the
text. It cannot observe that the model read it: an SDK may end its run on a tool result
without another model call (OpenAI Agents' `tool_use_behavior="stop_on_first_tool"`, Pydantic
AI output tools). "Claimed" is true in every case, and it names the API call that causes it.

**Why not a field on `tool_end`.** Activity tools publish `tool_end` from inside the activity
(`activity_tool_defn`'s activity body), before the result returns to the workflow. The claim
happens in the workflow afterwards, in the adapter. When `tool_end` is published the decision
has not been made, and the activity cannot read the workflow's queue. Putting it there would
mean moving `tool_end` publication out of activities for every tool. Instead,
`steering_claimed` carries the same `tool_id`, so a UI renders the steering under that tool's
card by joining the two events.

`tool_end.tool_output` stays the tool's own output, without the steering block, for the same
reason Code Mode host calls keep theirs: it is what the tool returned.

## End of turn: undelivered steering is dropped

When the last participant leaves and the turn closes, the runner clears any queued steering,
publishes `steering_dropped` for it, then publishes `turn_end`. Steering never carries into the
next turn: it is about the work in progress, and attached to the next turn's first tool result
it would arrive next to unrelated work.

This happens whenever the model finishes without another outermost tool call after the
steering arrived — a final answer, or a handler with no tool loop at all.

A handler that runs a model loop may claim leftovers itself before returning — for example
call `claim_steering()` after its SDK run and, if anything comes back, prompt again with the
render as user input. That is the author's choice; the harness does not re-prompt on its own,
because it does not know which participant runs the model loop.

**Client guidance** (to publish in the client docs and the packaged UI): a steering message
reported in `steering_dropped` never reached the model. The client should tell the user and
offer to resubmit it as a new message, which opens a turn of its own once the agent is idle.

## Behavior in the cases that need care

- **Parallel tool calls.** Several outermost calls from one model step resolve independently;
  the first to resolve after the steering arrived claims it. All of that step's results reach
  the model in the same request, so which one carries it does not matter to the model.
- **Several steers before a claim.** One block, all messages in arrival order;
  `steering_claimed.steering_ids` lists them all.
- **A call already in flight when steering arrives.** It carries the steering when it
  resolves. "Next tool result to resolve" is the contract, not "next tool call to start".
- **Long Code Mode scripts.** Steering waits until the script finishes and the `run` result
  returns. Code Mode cannot be interrupted at a host-call boundary without corrupting a value
  the script reads, so this is the earliest correct point.
- **Approval-gated tools.** A call waiting on approval resolves when the human answers (or
  the call is denied); either result reaches the model, so either can carry steering.
- **Concurrent model loops in one turn.** The queue belongs to the agent's runner, not to a
  participant. If two participants each run a model loop, whichever claims first gets the
  steering. Rare, and the events say which one (`steering_claimed`'s envelope `message_id`).
- **Subagents.** Steering a parent does not reach its children. A child's tools run on the
  child's runner and queue. A `subagent_toolset` call is an outermost tool call on the
  parent, so its result can carry the parent's steering. To steer a child, send the child a
  message.

## Alternatives considered

- **Attach inside `run_tool`.** Rejected: `run_tool` returns typed values, and it is also the
  path Code Mode's host calls take. Appending there corrupts structured results.
- **Make `steer()` awaitable until delivery.** Rejected: the waiting handler is itself a
  turn participant, so it would hold the turn open while waiting for an outcome that may only
  be decided at turn end.
- **A `steering` field on `tool_end`.** Rejected for the ordering reason above.
- **Pydantic AI `ToolReturn(content=...)`.** Pydantic AI can send extra content as a separate
  user message after the tool return, leaving structured results untouched. Rejected for
  consistency: every other path appends to the tool result, and steering should look the same
  to the model whichever SDK the agent uses.
- **Carry undelivered steering into the next turn.** Rejected; see End of turn.
- **A built-in steering handler.** Deferred, not rejected.

## TypeScript

Same design, same names in camelCase, at the mirrored paths:

- `steer` / `claimSteering` on the runner in `core/src/harness/agent-workflow.ts`. The
  outermost-only check reads `toolCallStore.getStore()`, which is what `runTool` sets.
- The three event types come from `contract/protocol.schema.json` through the generated
  client `protocol.ts`, which `core/src/harness/agent-protocol/events.ts` re-exports.
- `ClaimedSteering` / `SteeringMessage` in `core/src/harness/steering.ts`, exported from the
  package root for hand-written loops.
- Adapter changes in `core/src/ai-sdks/openai-agents-harness.ts`, including its MCP wrapper.

## Implementation touch points

- `harness/agent_workflow.py`: the queue in runner state, `steer`, `claim_steering`, and the
  drop in the turn-completion path before `turn_end` is published.
- `harness/agent_protocol/events.py` and `events.ts`: `SteeringEnqueued`, `SteeringClaimed`,
  `SteeringDropped`; regenerate `contract/protocol.schema.json`.
- `ai_sdks/openai_agents_harness.py`, `ai_sdks/pydantic_ai_harness.py`, and the TS adapter.
- `harness/agent_protocol/agent_interface.py`: replace the `self._steering.append(...)`
  sketch in the `MessageContext` docstring with `self._runner.steer(...)`.
- The `code_mode_memory` and Monty chat examples, to show `claim_steering` in a loop the
  author owns.
- A conformance scenario with goldens for both outcomes: steering claimed by the next tool
  result, and steering dropped at turn end.
- The client projection: each message's `steering` list, settled by the claim and drop events.
- Tests: a Code Mode script running while steering is queued, asserting the host calls'
  results are unchanged and the `run` result carries the block; parallel tool calls claiming
  once.
- The packaged UI: show `steering_enqueued` under its message, mark it claimed (linked to the
  tool card when `tool_id` is set) or dropped, with the resubmit offer.

## What changed on the way in

- **`ClaimedSteering.append_to` / `appendTo`.** Every caller appended the render after a blank
  line, so the value type does it once.
- **The examples steer through `ask`.** `code_mode_memory` and the Monty chat agents (inline and
  subagent, in both SDKs) claim steering in their own Gemini loops. There is no separate steering
  handler: `ask` is `MidTurn.ACCEPT`, opens a turn when the agent is idle, and steers the open
  turn when it is busy, where it used to queue behind it.
- **An MCP server that raises carries no steering.** The exception travels on to the SDK with no
  result to attach to, so the steering stays queued for the next outermost result. A denied call
  or an `is_error` result is a result, and carries it.
- **Conformance.** The scripted integration gained a `steer` step, and its `tool` step claims
  steering after `run_tool` as an adapter does; with nothing queued that publishes nothing, so
  no existing golden changed. The golden normalizer numbers `steering_id` and each entry of
  `steering_ids` as `steering-<n>`.
- **The client projection** gives each message a `steering` list (text, status, carrying
  `toolId`). The packaged UI folds the same events into its own transcript, with a one-click
  resend for dropped steering.
- **Reactive steering state in the client bindings.** Every `AgentView` carries `turnNumber` and
  `steering`: `queued`, `claimed` and `dropped` for the agent's latest turn only, emptied when the
  next turn opens. Each entry pairs the message the user sent (`messageId`, `handler`, typed
  `input`) with what the handler queued (`steeredText`), because a handler may rewrite or add to
  the text before steering and a UI should normally render what the user wrote. React exposes the
  root agent's as `session.steering` and `useSteering`; Svelte as a derived `session.steering`.
  Lists and entries keep their identity until a steer changes, so neither re-renders on streamed
  reply text. No resend action and no callbacks: entries carry the original send, and reactive
  state covers what a callback would.
