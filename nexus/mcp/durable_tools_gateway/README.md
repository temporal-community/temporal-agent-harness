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
namespace reachable by its Nexus endpoint. `external_http` registrations use the small
`/sessions`, `/sessions/{id}/turns`, and `/close` protocol as a feasibility proof; the
harness-native Nexus path is the primary implementation.

## Polling behavior

Nexus has no native attach primitive, so `BrokeredAgentAttach` long-polls `pollMessages`.
One bounded workflow owns the loop for a browser attachment, publishes batches through a
local activity, and stops as soon as the agent is idle and caught up. It checks status only
after an empty poll or a terminal event, avoiding both per-event status calls and the race
where status becomes idle just before `turn_end` is published. Long-running attachments
continue as new after 500 polls.

The Nexus operation uses the harness's stream-poll update while the target workflow is
running. If Temporal reports that the workflow has already completed, `pollMessages` reads
the same bounded page through the harness's replay query instead. Both paths return identical
stream items and cursors, so stopped native subagents retain their mountable UI history.
