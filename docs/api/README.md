# Harness API and session lifecycle

The shared Python server serves the same HTTP API and browser console for Python and TypeScript
agents. Run a Temporal service, session-manager worker, API server, and your agent worker with
the same connection and namespace. Worker task queues must match the registry.

## Discover and start

`GET /api/agents` lists registered workflow types, init-data schemas, and worker poller status.
A poller indicates availability on a queue, not proof that it hosts a particular workflow type.
`GET /api/sessions` lists sessions. Create one using the **workflow type**, not the registry key:

```bash
curl --fail-with-body http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"agent_workflow_type":"OpenAIHelloAgentTs"}'
```

The response's `workflow_id` is the session ID used by subsequent calls. An optional `session_id`
lets an integration derive a stable ID from its own conversation identity; repeated creation
with the same ID is idempotent. Supply `data` when the agent requires init data. Do not send it
for an agent that takes none. The server checks presence and the agent validates its schema.
Read `GET /api/agent-interface/{session_id}` for handlers and their input/output schemas.

## Send and observe separately

Use `POST /api/messages` for typed messages:

```json
{
  "session_id": "the-workflow-id",
  "message": {"type": "ask", "payload": {"text": "What's the weather in Paris?"}},
  "request_id": "stable-id-for-this-one-delivery"
}
```

An accepted message may open a turn, join a running turn, or be queued. Acceptance is not
completion. Save the returned `message_id` and follow the event stream for its result. Reuse
`request_id` only when retrying the same delivery; distinct user messages need distinct IDs.

Attach with `GET /api/attach?session_id=...&from_offset=0` (URL-encode the ID). The response is
SSE (`text/event-stream`). Events carry JSON data, including `resume_offset` when available.
Save the last processed resume offset and pass it as `from_offset` on reconnect. Do not assume
browser `Last-Event-ID` is the resume mechanism. Read the event's handler/message/agent identity:
a session can contain queued messages, mid-turn handlers, and subagent events.

Use the published `@temporalio/agent-harness-client`, React, or Svelte clients when possible;
they implement stream/state handling. `POST /api/chat` is a convenience for a turn it opens;
use message submission plus attach for general application clients and mid-turn handlers.

## Human interaction

`POST /api/approve` takes `session_id`, `tool_id`, `approved`, optional `reason`, and optional
`remember`. An approval authorizes the pending tool; it does not provide a callback result.

`POST /api/callback-result` takes `session_id`, `tool_id`, and `result` or `error`. A callback
executes in the client environment. Preserve the identity of the agent requesting it; a UI may
be answering a subagent's request. The client SDK handles routing across the agent tree.

## State, status, and closing

Apply a state snapshot before its subsequent patches, in stream order. Use the SDK's state
implementation instead of treating every event as a complete state document.

- `GET /api/status/{session_id}`: current turn, pending work, and agent status.
- `GET /api/workflow-status/{workflow_id}`: execution status.
- `POST /api/sessions/{session_id}/close`: signal the agent to wind down and settle pending interactions.

Disconnecting an SSE stream does **not** close the agent. A network error is not evidence that
its workflow has ended. Reconnect; only treat an explicit not-found/closed result as terminal.

## Errors and schemas

Check HTTP status before parsing a success response. Invalid init-data presence returns 422;
other errors may contain `error`/`message` or FastAPI's `detail`. Do not retry a rejected payload
unchanged. On a transport failure, retry a submission using its original request ID.

The running server exposes `/openapi.json` and `/docs`, generated from its route declarations.
For offline tooling with the `ui` extra installed:

```bash
temporal-agent-harness schema --openapi -o openapi.json
```

Generate an agent's schema from its declaration:

```bash
# Python
temporal-agent-harness schema my_agent.workflow:OpenAIHelloAgentWorkflow -o agent.schema.json
# TypeScript: compile first (agent-harness-schema is the same command)
agent-harness schema ./dist/agents.js#OpenAIHelloAgentTs -o agent.schema.json
# Either schema can generate a typed application client
harness-codegen agent.schema.json -o Agent.ts
```

The code generator is distributed as `@temporalio/agent-harness-codegen`. Regenerate and commit
client types after changing handlers, state, or init data. Never patch generated types manually.

## Registry and deployment

`agents.toml` declares a key, workflow type, task queue, label, description, and optional schema
path relative to the TOML file. Keep the schema's agent name equal to `workflow_type`. The server
never imports your agent implementation. The session manager caches its registry: replacing the
registry file alone does not update an already-running manager's list. Restart with fresh local
Temporal state only when the development sessions are disposable.

The built-in local payload store is for processes sharing a host/filesystem. For a multi-host
deployment, configure a shared offload store consistently across clients, workers, and server.
Expose the API through your application's authentication boundary when deploying beyond local
development. Embedding the FastAPI app lets you add your own middleware and routes.
