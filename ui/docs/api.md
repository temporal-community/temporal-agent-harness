# Server API Contract

The FastAPI server is a thin facade over Temporal workflows. The browser does
not need real Temporal data to develop against it; a mock only needs to serve
the JSON endpoints below and produce SSE frames with the documented event
shapes.

Source of truth in the current server:

- `temporal_agent_harness/web/app.py`: HTTP routes and SSE flattening.
- `temporal_agent_harness/web/session_manager.py`: agent registry and session shapes.
- `harness/agent_protocol/events.py`: stream event payloads.
- `harness/agent_protocol/agent_interface.py`: status, accepted-message, and
  tool-approval shapes.

## Static Routes

### `GET /`

Returns the packaged Vite UI (`temporal_agent_harness/ui/dist/index.html`)
when no custom `static_dir` is supplied. In development, run the shared Svelte
app from `ui` with Vite and let it proxy `/api` to the same FastAPI server.

### `GET /states`

Only present when `create_agent_harness_app(..., states_file=...)` is used.
The packaged Vite UI does not register this route.

### `GET /static/*`

Serves the configured static asset directory. By default this is the packaged
Vite dist directory.

### `GET /assets/*`

Serves `static_dir/assets` when that directory exists.

The packaged UI uses relative asset and API URLs, so the app can be mounted at
`/` or under a path prefix as long as the API routes and static routes are
mounted together.

## JSON Endpoints

### `GET /api/agents`

Returns the launchable agent registry.

```ts
type AgentRegistryResponse = {
  agents: AgentDescriptor[]
}

type AgentDescriptor = {
  key: string
  workflow_type: string
  task_queue: string
  label: string
  description: string
}
```

There is no `default` field in the actual response. Agent defaulting is a UI
policy decision.

### `GET /api/sessions`

Returns all sessions tracked by the session manager.

```ts
type SessionsResponse = Session[]

type Session = {
  workflow_id: string
  created_at: number
  label: string
  agent_workflow_type: string
  is_message_queuing_enabled: boolean
  initial_user_message?: string | null
}
```

`created_at` is epoch seconds. `initial_user_message` is the first
`turn_started` message rendered for display when available.

### `POST /api/sessions`

Creates a session for one agent type.

```ts
type CreateSessionRequest = {
  agent_workflow_type: string
  is_message_queuing_enabled?: boolean
}

type CreateSessionResponse = Session
```

`agent_workflow_type` must be one of the `workflow_type` values returned by
`GET /api/agents`.

Session deletion is not currently exposed by the shared session-manager API.
The UI keeps the delete action disabled until the backend grows a terminate
route.

### `GET /api/status/{session_id}`

Returns workflow status for one session.

```ts
type AgentStatusResponse = {
  current_turn: number
  turn_active: boolean
  pending_turns: PendingTurn[]
  is_message_queuing_enabled: boolean
  pending_approvals: PendingApproval[]
  approval_policy: ToolApprovalPolicy
  has_custom_approval_fallback: boolean
}

type PendingTurn = {
  turn_number: number
  turn_id: string
  message: string
}

type PendingApproval = {
  tool_id: string
  tool_name: string
  tool_input: Record<string, unknown>
  turn_number: number
}

type ToolApprovalPolicy = {
  dangerously_skip_all_approvals: boolean
  auto_approve_inherently_safe: boolean
  auto_approve_tools: string[]
}
```

The Svelte app can call this endpoint to discover pending approvals and session
state outside the active stream. Verify server JSON serialization for
`approval_policy`; it is a nested Pydantic model inside a dataclass.

### `GET /api/agent-interface/{session_id}`

Returns the inbound message contract for the agent behind a session: **every**
`@agent.accepts` handler it declares.

```ts
type AgentInterfaceFunction = {
  name: string
  description: string
  parameters: Record<string, unknown>   // JSON Schema of the handler's input model
  output: Record<string, unknown>       // JSON Schema of its return model
  mid_turn: "enqueue" | "reject" | "accept"
  model_callable: boolean
}
```

This is the only discovery surface an agent has, and it is enough to render an
arbitrary agent: send the handler `name` as the message `type` and build
`payload` from its `parameters` schema. A client should not hardcode handler
names — there is no privileged handler, and no separate command channel.

`mid_turn` says what happens if the message arrives while a turn is already
open, so a client can tell the user whether sending will queue behind the
current work (`enqueue`), join it (`accept`), or be refused (`reject`). All
three behave identically when the agent is idle.

`model_callable` is the handler author's hint that a parent agent's model may
drive it. It is advisory: a parent's own `SubagentToolPolicy` decides what
actually reaches its model, so never treat this as an access decision.

The two harness-owned updates (`tool_approval`, `provide_callback_result`) are
absent because they are not handlers — they are resolved in-process and are
reachable only from an out-of-band control plane (`POST /api/approve`,
`POST /api/callback-result`).

### `POST /api/sessions/{session_id}/close`

Stops an agent via the harness `close` signal: it winds down its turn loop,
drains in-flight work, and auto-denies pending approvals and callbacks.

A control-plane action rather than a message, so it works on any agent whatever
it happens to accept. This is what a client uses to stop an agent — there is no
packaged stop *message*.

### `POST /api/approve`

Resolves a pending human approval for a gated tool call.

```ts
type ToolApprovalRequest = {
  session_id: string
  tool_id: string
  approved: boolean
  reason?: string | null
  remember?: boolean
}

type ToolApprovalResponse = {
  tool_id: string
  accepted: true
}
```

When `approved` and `remember` are both true, the workflow allow-lists the tool
for the rest of the session. The stream later emits `tool_approval_resolved`,
and on approval normally continues with `tool_start` and `tool_end`.

### `POST /api/chat`

Submits a message and streams events through completion of the submitted turn.

```ts
type ChatRequest = {
  session_id: string
  message: string | AgentMessageObject
  expected_turn: number
}

type AgentMessageObject = {
  type: string
  [key: string]: unknown
}
```

Response media type: `text/event-stream`.

The server performs the workflow update before streaming begins. If the update
is rejected, the response is a regular HTTP error instead of an SSE stream.
The stream is already merged: it contains the root agent and every recursive
subagent event in one ordered sequence.

The client does not pass a stream offset to `POST /api/chat`; the workflow
acceptance response determines the exact turn-start offset internally.

### `POST /api/messages`

Submits a message without opening a turn stream.

```ts
type SubmitMessageResponse = {
  turn_number: number
  turn_id: string
  accepted_offset: number
  pending: boolean
}
```

**Tracking `expected_turn`.** Set the next value from the reply's `turn_number + 1`, not
from a count of messages sent. A message whose handler declares `mid_turn: "accept"` joins
the turn already in flight, so the agent's counter does not advance and the next message must
claim the same number again — a per-send increment over-counts on the first join and then
fails every later send with a 409 `stale_turn`. `GET /api/status/{session_id}` re-derives the
correct value (`current_turn + pending_turns.length + 1`) if a client loses track.

The shared UI uses this endpoint for queued sends, then keeps one
`GET /api/attach` stream open from its last `resume_offset`. This avoids
starting many concurrent Temporal Updates and long-lived merged streams when a
user sends several queued messages quickly.

Message payloads are built from the target handler's `parameters` schema, which
`GET /api/agent-interface/{session_id}` returns. There is no command channel and
no reserved message type — reconfiguring an agent mid-session is just a message
to a handler that agent declares. For example, an agent exposing a `set_model`
handler whose input model is `{"model": Literal["a", "b"]}` is driven with:

```json
{
  "type": "set_model",
  "payload": { "model": "gemini-3.1-flash-lite" }
}
```

Because the constraint lives in the JSON Schema (as an `enum`), a client can
render it as a dropdown and the workflow enforces it at the update boundary.

The packaged UI keeps a familiar affordance on top of this: typing `/` in the
composer opens a picker over the handlers `agent_interface` returned. That is
purely a client-side convention — the harness has no notion of a slash.

### `GET /api/attach?session_id=...&from_offset=0`

Replays or tails an existing merged session stream.

Response media type: `text/event-stream`.

When `from_offset` is `0`, the server replays the merged session event history.
When it is non-zero, pass a prior frame's `resume_offset`; only newer root-stream
positions are streamed. The stream returns when the workflow is idle and caught
up. `resume_offset` is a root-stream cursor, not a display ordinal: several
subagent frames may carry the same value.

## Error Responses

Known application errors are shaped as:

```ts
type ApiErrorResponse = {
  error: string
  message: string
}
```

Known status codes:

- `409` from `POST /api/chat`: `error` is `stale_turn` or `agent_busy`.
- `409` from `POST /api/approve`: `error` is usually
  `UnknownToolApproval` or `ToolApprovalAlreadyResolved`.
- `422` from FastAPI/Pydantic validation: standard FastAPI validation payload.

## SSE Transport

Each frame is:

```txt
event: <event_type>
data: <json>

```

For normal agent events, `data` is a flat object containing:

- the event payload fields
- `type`, matching the SSE event name
- `agent_id`, identifying the agent that published the event
- `turn_id`
- `turn_number`
- `timestamp` epoch seconds
- `resume_offset`, the root-stream cursor the client should pass as `from_offset`

Example:

```txt
event: reply_delta
data: {"type":"reply_delta","agent_id":"root","turn_id":"t1","turn_number":1,"timestamp":1710000001,"resume_offset":2,"text":"Hi"}

```

`POST /api/chat` can also emit client-side `error` frames for timeout or agent
turn failure. Those frames have `kind`, `message`, and `resume_offset`, but may
not have `type` or turn metadata.

## SSE Event Payloads

All normal payloads include the metadata described above.

```ts
message_queued: {
  user_message: string
}

turn_started: {
  user_message: string
}

turn_end: {}

model_interaction_started: {
  model: string | null
}

model_interaction_ended: {
  model: string | null
  usage: TokenUsage | null
}

tool_requested: {
  tool_id: string
  tool_name: string
  tool_input: Record<string, unknown>
}

tool_approval_requested: {
  tool_id: string
  tool_name: string
  tool_input: Record<string, unknown>
}

tool_approval_resolved: {
  tool_id: string
  tool_name: string
  approved: boolean
  reason: string | null
  remember: boolean
}

tool_start: {
  tool_id: string
  tool_name: string
  tool_input: Record<string, unknown>
}

tool_progress_delta: {
  tool_id: string
  tool_name: string
  progress_delta: string
}

tool_end: {
  tool_id: string
  tool_name: string
  tool_output: string
}

tool_error: {
  tool_id: string
  tool_name: string
  message: string
}

subagent_started: {
  subagent_id: string
  agent_key: string
  workflow_id: string
}

subagent_message_sent: {
  subagent_id: string
  agent_key: string
  workflow_id: string
  function: string
  subagent_turn: number
  from_offset: number
}

subagent_reply_received: {
  subagent_id: string
  agent_key: string
  workflow_id: string
  function: string
  subagent_turn: number
  outcome: "ok" | "error"
}

subagent_stopped: {
  subagent_id: string
  agent_key: string
  workflow_id: string
}

subagent_stream_unavailable: {
  subagent_id: string
  workflow_id: string
  reason: string
}

reply_delta: {
  text: string
}

thought_summary: {
  delta: Record<string, unknown>
}

text_annotation: {
  delta: Record<string, unknown>
}

reply: {
  output: Record<string, unknown>
}

error: {
  message: string
}
```

Token usage:

```ts
type TokenUsage = {
  input_tokens?: number | null
  output_tokens?: number | null
  thought_tokens?: number | null
  cached_tokens?: number | null
  tool_use_tokens?: number | null
}
```

## Minimal Mock Stream

A basic text turn can be mocked as:

```txt
event: turn_started
data: {"type":"turn_started","agent_id":"root","turn_id":"t1","turn_number":1,"timestamp":1710000000,"resume_offset":1,"user_message":"hello"}

event: reply_delta
data: {"type":"reply_delta","agent_id":"root","turn_id":"t1","turn_number":1,"timestamp":1710000001,"resume_offset":2,"text":"Hi"}

event: reply
data: {"type":"reply","agent_id":"root","turn_id":"t1","turn_number":1,"timestamp":1710000002,"resume_offset":3,"output":{"text":"Hi there."}}

event: turn_end
data: {"type":"turn_end","agent_id":"root","turn_id":"t1","turn_number":1,"timestamp":1710000003,"resume_offset":4}

```

For a queued-message mock, emit `message_queued` immediately after accepting the
message, then later emit `turn_started` with the same `turn_id` and
`turn_number`.

For an approval mock, emit `tool_requested`, `tool_approval_requested`, wait for
`POST /api/approve`, then emit `tool_approval_resolved`. If approved, continue
with `tool_start` and `tool_end`; if denied, do not emit `tool_start`.
