# ui_connector

Connects chat platforms (Slack, Teams, ...) to the temporal-agent-harness agent, through
one Temporal workflow: `RouterWorkflow`.

## Data flow

```
platform event -> inbound (webhook) -> RouterWorkflow -> backend (agent) -> RouterWorkflow -> outbound (post/stream reply)
```

1. **inbound** receives the platform event over HTTP and starts `RouterWorkflow` via
   `client.ExecuteWorkflow`.
2. `RouterWorkflow.Run` calls **backend**.`StartTurn` to get a reply (or a handle to poll).
3. If a handle came back, `RouterWorkflow` polls **backend**.`PollTurn` in a loop and
   forwards each delta to **outbound**, which posts/streams it back to the platform.

`router` never inspects the platform-specific shape of a request/response - that's
entirely the job of `backend` (interpreting input) and `outbound` (rendering deltas).

## Directory layout

One folder per platform. Contracts + orchestration live in `router/`.

```
router/            core: workflow + the two ports + shared request type
  workflow.go        RouterWorkflow.Run, WorkflowName, RouterWorkflowID
  interfaces.go       OutboundDriver, BackendDriver (the two ports)
  wire.go             Input (the RouterWorkflow argument type)

slack/             everything Slack
  bot.go             SlackBot: shared auth bootstrap (used by both binaries below)
  inbound/           pkg slackinbound - webhook HTTP server -> starts RouterWorkflow
  outbound/          pkg slackoutbound - OutboundDriver impl + SlackPlatform (real API calls)
  cmd/{webhook,worker}/  the two binaries
  slack_app_manifest.yaml

teams/             everything Teams (same inbound/outbound split)
  inbound/           pkg teamsinbound
  outbound/          pkg teamsoutbound - dispatches to the Python Teams activity worker
                     (no Go SDK for Teams)
  cmd/{webhook,worker}/  the two binaries

agent/             the one BackendDriver impl: Nexus caller into temporal-agent-harness
```

**Dependency direction:** `slack/*`, `teams/*`, `agent` all import `router`. `router`
imports nothing platform-specific. Never add a `router` -> platform import.

**Two binaries per platform**, under that platform's own folder (`slack/cmd/`, `teams/cmd/`):
- `cmd/webhook` - HTTP server + Temporal client only. No workflow/worker.
- `cmd/worker` - registers `RouterWorkflow` + the outbound driver's activities.

## Writing a new driver (e.g. Discord)

You're implementing one or both ports in `router/interfaces.go`.

### 1. `OutboundDriver` - deliver replies to the platform

```go
type OutboundDriver interface {
    SupportsStreaming(input Input) bool
    BeginStream(ctx workflow.Context, input BeginStreamInput) (StreamHandle, error)
    UpdateStream(ctx workflow.Context, input UpdateStreamInput) error
    FinishStream(ctx workflow.Context, input FinishStreamInput) error
    PostMessage(ctx workflow.Context, input TextMetadata) error
    PostApprovalPrompt(ctx workflow.Context, input ApprovalPromptInput) error
    AcknowledgeApproval(ctx workflow.Context, input ApprovalAcknowledgementInput) error
}
```

- `SupportsStreaming` = false -> router calls `PostMessage` once with the full text
  instead of streaming. Use this if the platform can't do incremental edits.
- Real platform I/O is non-deterministic -> **must run as Activities**, not directly in
  the interface methods. Pattern (see `slack/outbound/driver.go`):
  - `Driver` struct: thin dispatcher, only calls `workflow.ExecuteActivity`.
  - Separate `Platform` struct: the actual SDK calls, registered as activities via your
    own `RegisterActivities(w worker.Worker, platform *Platform)`.
- `AcknowledgeApproval` can be a no-op if your platform resolves the prompt some other
  way (Slack does - see its comment for why).
- `PostApprovalPrompt` posts approve/deny buttons. The decision comes back through
  **your inbound webhook**, not through this interface - you decode it and call
  `client.ExecuteWorkflow` with `Input.Approval` set.

### 2. `BackendDriver` - only needed if you're adding a new agent backend, not a new platform

```go
type BackendDriver interface {
    StartTurn(ctx workflow.Context, input Input) (StartResult, error)
    PollTurn(ctx workflow.Context, handle TurnHandle, cursor int64) (PollResult, error)
}
```

All input interpretation (what a slash command means, how approvals resolve) lives
here. Router just forwards `Input` unexamined. See `agent/driver.go`.

### 3. Inbound - no interface, just a convention

Not an interface because it starts the `RouterWorkflow` via `client.ExecuteWorkflow`, not something `router` calls back into. In our example drivers, these are webhooks that:
1. Parses the platform's HTTP payload.
2. Builds a `router.Input` (`Message`, `Slash`, or `Approval` - exactly one non-nil).
3. Calls `tc.ExecuteWorkflow(ctx, client.StartWorkflowOptions{ID: wfID, TaskQueue: ...}, router.WorkflowName, input)`.
   Use `router.RouterWorkflowID(identity, sessionID, interactionID)` for `wfID`.

See `slack/inbound/server.go` or `teams/inbound/server.go`. HTTP is not the only way to write an inbound driver, these examples only serve to demonstrate how we can map HTTP to Temporal primitives.

### 4. Wire it up

Put both binaries under your new platform's own folder: `yourplatform/cmd/worker/main.go`
and `yourplatform/cmd/webhook/main.go` (see `slack/cmd/` for the pattern).

In `yourplatform/cmd/worker/main.go`:
```go
outboundDriver := yourplatform.NewDriver(...)
backendDriver := &agent.Driver{}
w := worker.New(tc, taskQueue, worker.Options{})
rw := router.NewRouterWorkflow(outboundDriver, backendDriver)
w.RegisterWorkflowWithOptions(rw.Run, workflow.RegisterOptions{Name: router.WorkflowName})
yourplatform.RegisterActivities(w, platform) // if you have real Activities to register
```

In `yourplatform/cmd/webhook/main.go`: just the HTTP server + Temporal client, pointing at
the same `taskQueue`.

## Gotchas

- Streaming can be interrupted mid-turn by a tool-approval prompt. If your platform
  can't post a message while a stream is open, set `StreamHandle.CloseBeforeApproval =
  true` in `BeginStream` - router will finish the stream before posting the prompt and
  reopen it after.
