# Accepted Messages

An agent's inbound surface is just its `@agent.accepts` handlers. There is no
separate command channel, no reserved message type, and nothing the harness
knows how to do on an agent's behalf — a control action is an ordinary handler
with a typed input model:

```python
class SetModel(BaseModel):
    """Which model this session should use for subsequent turns."""

    # A Literal, not a bare str: pydantic enforces it at the update boundary AND emits it as
    # an enum in the handler's `parameters` schema, so a generic client renders a dropdown.
    model: Literal["gemini-3.5-flash", "gemini-3.1-flash-lite"]


@agent.defn
class TravelAgent:
    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def plan(self, msg: PlanTrip) -> Itinerary:
        """Plan a trip."""
        ...

    @agent.accepts(mid_turn=MidTurn.ACCEPT, model_callable=False)
    async def set_model(self, msg: SetModel) -> TextReply:
        """Set the model this session uses for subsequent turns."""
        self._model = msg.model
        return TextReply(text=f"Model set to {msg.model}.")
```

Each handler declares two things:

**`mid_turn`** — what happens if the message arrives while a turn is already
open. It is per handler because "a config change must work on a busy agent" and
"a user message should queue" are the same decision made oppositely, and no
single answer serves both. All three modes behave identically when the agent is
idle.

| Mode | Mid-turn arrival |
| --- | --- |
| `MidTurn.ENQUEUE` | Queues behind the open turn; runs as its own turn later. |
| `MidTurn.REJECT` | Fails the update with a typed `MidTurnRejected` error. |
| `MidTurn.ACCEPT` | **Joins** the open turn and runs concurrently with it. |

`REJECT` is the default: it is the only mode that cannot silently surprise you.
Queuing delays work you thought you had dispatched, and joining opts the handler
into concurrency with the running turn — an `ACCEPT` handler runs alongside that
turn *and* alongside other `ACCEPT` handlers, so any agent state it mutates is
shared and a read-modify-write across an `await` can lose an update.

Because a turn is the interval the agent is non-idle rather than the span of one
message, `turn_end` fires when the turn's **last** participant finishes — making
it a true quiescence signal a client can disconnect on.

Two ids on the event stream keep that legible. `turn_id` is the busy interval;
`message_id` is one message's dispatch, minted at admission, returned on the send
(`AgentMessageReply.message_id`), and stamped on **every** event that message
causes — its deltas, its tool calls, its reply. A consumer pairs a reply with what
asked for it by `message_id`, never by `turn_id`: two `ACCEPT` handlers share one
`turn_id`, and keying on the turn merges them together.

```
message_accepted{handler, payload, disposition}   # admission — the only event carrying the message
turn_started                                       # the agent went busy (message_id: null)
  message_handler_start                            # this message's handler began
    reply_delta · tool_start · tool_end · …        # everything it causes, all attributed
  message_handler_end{output}                      # it returned  (or message_handler_error)
turn_end                                           # the agent went idle again (message_id: null)
```

`disposition` says what the message did to the turn — `opened` it, `joined` the
one already running, or `queued` behind it — so a client knows what it got
without inferring it from the turn counter.

**`model_callable`** (default `True`) — a hint that a parent agent's model may
drive this handler. Only a hint: the parent's `SubagentToolPolicy` decides, and
may honor it, narrow it, or ignore it:

```python
agent.subagent_toolset(
    ChildAgent, key="researcher", task_queue=...,
    tools=agent.SubagentToolPolicy.allow_model_callable(),   # default: honor the hints
    # tools=agent.SubagentToolPolicy.allow_only("ask"),      # narrower than the hints
    # tools=agent.SubagentToolPolicy.dangerously_allow_all() # ignore them entirely
)
```

A handler that wants to know whether it opened its turn or joined one asks for
the context, which the workflow supplies and the model never sees:

```python
@agent.accepts(mid_turn=MidTurn.ACCEPT)
async def user_text(self, msg: UserText, ctx: agent.Injected[MessageContext]) -> Reply:
    if ctx.joined_turn:
        self._steering.append(msg.text)   # drained by the running model loop
    else:
        return await self._prompt(msg.text)
```

### Discovery

The `agent_interface` query returns **every** handler — name, docstring, the
input/output JSON schemas, `mid_turn`, and `model_callable`. Nothing is hidden
from it: what a parent model can reach is decided by which tools its toolset was
built with, not by what a query admits to, so filtering there would only be
security-by-obscurity while costing a debugging UI the ability to see an agent's
full surface.

That is enough for a client to drive an arbitrary agent with no hardcoded
handler names — which is exactly what the packaged UI does. Human/operator
actions that are *not* messages stay first-class and separate: approve or deny a
gated tool call (`tool_approval`, whose `remember` flag also relaxes the live
policy), read state (`agent_status`), and stop the agent (the `close` signal).
