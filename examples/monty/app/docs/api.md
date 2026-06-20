# Server API Contract

The FastAPI server is a thin facade over Temporal workflows. The browser does
not need real Temporal data to develop against it; a mock only needs to serve
the JSON endpoints below and produce SSE frames with the documented event
shapes.

Source of truth in the current server:

- `examples/session_manager/app.py`: HTTP routes and SSE flattening.
- `examples/session_manager/workflow.py`: agent registry and session shapes.
- `harness/agent_protocol/events.py`: stream event payloads.
- `harness/agent_protocol/agent_interface.py`: status, accepted-message, and
  tool-approval shapes.

## Static Routes

### `GET /`

Returns the legacy static chat UI from the shared session manager. In
development, run this Svelte app with Vite and let it proxy `/api` to the same
FastAPI server.

### `GET /states`

Returns the same Svelte app; the frontend handles the selected diagnostics view.

### `GET /static/*`

Serves legacy/static assets from `examples/session_manager/static`.

### `GET /assets/*`

Not used by the current Monty Vite development flow.

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

Returns the inbound message contract for the agent behind a session, as a list
of accepted handler functions.

```ts
type AgentInterfaceFunction = {
  name: string
  description: string
  parameters: Record<string, unknown>
  output: Record<string, unknown>
}
```

Plain text is represented by an `ask` function that accepts a `text` field. For
typed messages, send the handler name as the message `type` and the input model
as `payload`.

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
  from_offset?: number
}

type AgentMessageObject = {
  type: string
  [key: string]: unknown
}
```

Response media type: `text/event-stream`.

The server performs the workflow update before streaming begins. If the update
is rejected, the response is a regular HTTP error instead of an SSE stream.

### `GET /api/attach?session_id=...&from_offset=0`

Replays or tails an existing session stream.

Response media type: `text/event-stream`.

When `from_offset` is `0`, the server replays the session event history. When it
is non-zero, only newer events are streamed. The stream returns when the workflow
is idle and caught up.

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
- `turn_id`
- `turn_number`
- `timestamp` epoch seconds
- `offset`, the next stream offset the client should pass as `from_offset`

Example:

```txt
event: reply_delta
data: {"type":"reply_delta","turn_id":"t1","turn_number":1,"timestamp":1710000001,"offset":2,"text":"Hi"}

```

`POST /api/chat` can also emit client-side `error` frames for timeout or agent
turn failure. Those frames have `kind`, `message`, and `offset`, but may not
have `type` or turn metadata.

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
data: {"type":"turn_started","turn_id":"t1","turn_number":1,"timestamp":1710000000,"offset":1,"user_message":"hello"}

event: reply_delta
data: {"type":"reply_delta","turn_id":"t1","turn_number":1,"timestamp":1710000001,"offset":2,"text":"Hi"}

event: reply
data: {"type":"reply","turn_id":"t1","turn_number":1,"timestamp":1710000002,"offset":3,"output":{"text":"Hi there."}}

event: turn_end
data: {"type":"turn_end","turn_id":"t1","turn_number":1,"timestamp":1710000003,"offset":4}

```

For a queued-message mock, emit `message_queued` immediately after accepting the
message, then later emit `turn_started` with the same `turn_id` and
`turn_number`.

For an approval mock, emit `tool_requested`, `tool_approval_requested`, wait for
`POST /api/approve`, then emit `tool_approval_resolved`. If approved, continue
with `tool_start` and `tool_end`; if denied, do not emit `tool_start`.
