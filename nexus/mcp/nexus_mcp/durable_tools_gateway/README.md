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

## Account UI

Run the shared Go UI tunnel in the gateway namespace, then serve the account UI from the
gateway worker:

```sh
CONNECTOR_NAMESPACE=gateway just ui-tunnel

TEMPORAL_NAMESPACE=gateway \
GATEWAY_UI_ACCOUNT_ID=account-123 \
GATEWAY_SEED_ACCOUNT_ID=account-123 \
GATEWAY_SEED_AGENTS='[{"agent_id":"qa","kind":"harness_nexus","label":"QA","description":"QA agent","nexus_endpoint":"qa-agent-endpoint"}]' \
uv run --extra nexus-mcp python -m nexus_mcp.durable_tools_gateway.worker
```

`GATEWAY_UI_PORT` defaults to `8000`. A registered native agent can live in any
namespace reachable by its Nexus endpoint. `external_http` registrations use the small
`/sessions`, `/sessions/{id}/turns`, and `/close` protocol as a feasibility proof; the
harness-native Nexus path is the primary implementation.

## UI transport

The gateway UI is another driver of the shared `UIAgentTunnelWorkflow`; it does not own a
second attach workflow or an in-process event broker. The Go tunnel performs one repeated
`A2AService.SubscribeToTask` Nexus operation per agent task and multicasts the untouched A2A
records to every mounted UI. Each browser has an independent cursor, and a lagging browser
replays from the agent's durable A2A stream. See `nexus/ui_connector/README.md` for the common
tunnel and driver contract.

Native sends enter through the shared tunnel's A2A path. One-shot status, interface,
approval, callback, command, and close controls remain standalone Nexus operations.
Third-party HTTP start, turn, and close requests run as standalone activities.
`BrokeredAgentDiscovery` remains a workflow because it drains multiple retained pages and
then reconciles the resulting child-session snapshot.
