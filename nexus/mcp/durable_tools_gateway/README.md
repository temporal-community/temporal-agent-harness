# Account gateway and brokered agent UI

The gateway stores one durable registry workflow per `account_id`. An account owns its
external MCP servers, subagent providers, mountable agents, and UI sessions. The UI never
uses Temporal visibility or the agent's namespace: it resolves a session through the
account registry and reaches a harness-native agent through the registered Nexus endpoint.

Harness code opts into the same toolbox explicitly:

```python
gateway = nexus_gateway("account-123")
mcp_server = gateway.mcp_servers("weather")
subagents = agent.nexus_subagent_gateway("account-123")
```

Until authentication is added, matching `account_id` values are the trust boundary.

## Colocated UI

Run the UI and gateway worker in one process so the attach workflow can publish streamed
Nexus batches to the browser connection without another pub/sub service:

```sh
TEMPORAL_NAMESPACE=gateway \
GATEWAY_UI_ACCOUNT_ID=account-123 \
GATEWAY_SEED_ACCOUNT_ID=account-123 \
GATEWAY_SEED_AGENTS='[{"agent_id":"qa","kind":"harness_nexus","label":"QA","description":"QA agent","nexus_endpoint":"qa-agent-endpoint"}]' \
uv run --extra nexus-mcp python -m durable_tools_gateway.worker
```

`GATEWAY_UI_PORT` defaults to `8000`. A registered native agent can live in any
namespace reachable by its Nexus endpoint. Every agent registration carries a standard
A2A `AgentCard`: native cards select the Temporal Nexus binding and external cards select
standard A2A HTTP+JSON.

The account bar is the owner-facing pane of glass: it shows registered agents, live and
historical session counts, MCP servers, and subagent providers. Selecting **Mount** creates
an account-owned session for that agent and attaches the shared UI to its registered
endpoint; no session-manager workflow or agent-namespace visibility access is involved.

## Polling behavior

Nexus operations do not yet return a server stream, so `BrokeredAgentAttach` repeatedly
invokes the bounded Nexus binding for A2A `SubscribeToTask`.
One bounded workflow owns the loop for a browser attachment, publishes batches through a
local activity, and stops as soon as the agent is idle and caught up. It checks status only
after an empty poll or a terminal event, avoiding both per-event status calls and the race
where status becomes idle just before `turn_end` is published. Long-running attachments
continue as new after 500 polls. Agent-service responses are paged at about 256 KiB before
crossing the Nexus and activity boundaries, and browser disconnects cancel their attach
workflow so stale long polls cannot accumulate.

The Nexus operation uses the harness's stream-poll update while the target workflow is
running. If Temporal reports that the workflow has already completed, it reads the same
bounded A2A page through the harness's replay query instead. Both paths return identical
stream items and cursors, so stopped native subagents retain their mountable UI history.

One-shot A2A calls and harness-only status, approval, callback, and operator controls do
not create proxy workflows; the gateway executes them as standalone Nexus operations.
Third-party A2A HTTP requests run in standalone activities. `BrokeredAgentDiscovery`
remains a workflow because it drains multiple retained pages and then reconciles the
resulting child-session snapshot.
