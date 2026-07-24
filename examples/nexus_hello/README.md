# Nexus-transport hello-world agent

Demonstrates `nexus_transport_mcp_server`: a two-tool OpenAI Agents SDK agent whose tools live
entirely outside the workflow, reached over Nexus. `workflow.py`'s `Agent(...)` never mentions
Nexus at all — `worker.py`'s `OpenAIAgentsPlugin(nexus_transport=True)` appends a working
Nexus-transport MCP server to every `Agent` automatically.

- `demo_get_fun_fact` — a 3rd-party (non-Nexus) MCP server, reached through the **Durable Tools
  Gateway**. `WorkflowTransport` doesn't know it directly, so it falls back to the gateway's
  `RegistryService.call_tool`, which starts `ToolCallWorkflow` on the caller's behalf.
- `demo-nexus_get_lucky_number` — a **Nexus-native** MCP server, called directly via
  `workflow.create_nexus_client()` — no gateway, no activity.

Both are registered the same way: a live `register_mcp_server` signal against the agent's own
`NexusMcpServerRegistry`, self-serve, per session (see "Run it"). `WorkflowTransport` tells a
direct server from a gateway/proxy apart structurally, from what each one's `list_tools`
returns — nothing to declare at registration time.

Three Temporal namespaces, so Nexus is brokering real namespace boundaries:

| Namespace | Hosts |
|---|---|
| `default` | The agent (`worker.py`), session-manager, FastAPI/UI. |
| `gateway` | The Durable Tools Gateway. |
| `nexus-mcp-server` | The demo Nexus-native tool service. |

## Layout

| File | Role |
|---|---|
| `workflow.py` | `NexusHelloAgentWorkflow` — one `ask` handler; `Agent(...)` mentions Nexus nowhere. |
| `worker.py` | Worker on `default`. `OpenAIAgentsPlugin(nexus_transport=True)` is the only Nexus-related line. |
| `tool_server.py` | Demo 3rd-party MCP server (`get_fun_fact`), registered with the gateway. |
| `nexus_tool_service.py` | Demo Nexus-native MCP server (`get_lucky_number`), built on `authoring.MCPOverNexusServiceHandler`. |
| `agents.toml` | Registry entry for the shared web UI. |

## Run it

Prereqs:
- From the repo root, `cp .env.example .env.local` and set `OPENAI_API_KEY`.
- `nexus-mcp` extra needs Python >=3.13 (`uv sync --extra nexus-mcp`, or just `uv sync` on 3.13+).
- `git` and `go` — `just temporal`/`register-tool` build a `temporal` CLI from source
  automatically (`just cli-build`, cached after the first run) since `register-tool` needs the
  `nexus operation` command family, not in the stable public CLI yet.

Each in its own terminal, in order:

```sh
just temporal             # 1. local Temporal dev server
just setup-nexus          # 2. ONE-SHOT: 3 namespaces + 2 Nexus endpoints
just registry             # 3. durable tools gateway
just tool-server          # 4. demo 3rd-party MCP tool server
just register-tool        # 5. ONE-SHOT: registers the tool server via a Nexus op call
just nexus-tool-service   # 6. demo Nexus-native tool service
just session-manager      # 7. session-manager worker
just server               # 8. builds UI, serves API + UI on :8000
just worker               # 9. this agent's worker
```

Wait for `just register-tool`'s `Status COMPLETED` (or `just registry`'s printed
`Successfully registered external MCP server 'demo'`).

Open http://localhost:8000, pick **Nexus Hello**, start a chat. Neither tool works yet — each
needs registering against *this conversation's* workflow, live:

```sh
temporal workflow list --namespace default   # find this session's workflow id
just register-gateway <workflow-id>          # 10. ONE-SHOT, per session
just register-nexus-tool <workflow-id>       # 11. ONE-SHOT, per session
```

Ask again — no restart, each takes effect immediately (try one at a time if you like).

```
demo_get_fun_fact:          default -> RegistryService (gateway) -> ToolCallWorkflow -> tool_server.py (HTTP)
demo-nexus_get_lucky_number: default -> nexus_tool_service.py (nexus-mcp-server namespace)
```

Without `just` (from the repo root; run `just -f examples/nexus_hello/justfile cli-build` once
first to get a `temporal` CLI with the `nexus operation` command family, installed to
`$(go env GOPATH)/bin/temporal`):

```sh
TEMPORAL_NAMESPACE=gateway uv run --extra nexus-mcp --group examples python -m durable_tools_gateway.server
uv run --extra nexus-mcp python -m examples.nexus_hello.tool_server

temporal nexus operation execute \
    --endpoint mcp-registry-endpoint --service RegistryService --operation register_external \
    --operation-id "nexus-hello-register-demo-$(date +%s)" \
    --input '{"name": "demo", "url": "http://127.0.0.1:8765/mcp"}'

TEMPORAL_NAMESPACE=nexus-mcp-server uv run --extra nexus-mcp python -m examples.nexus_hello.nexus_tool_service
uv run --group examples python -m examples.session_manager_worker
uv run --group examples python -m examples.app examples/nexus_hello/agents.toml --host 0.0.0.0 --port 8000
uv run --extra nexus-mcp --group examples python -m examples.nexus_hello.worker

# after starting a chat and finding its workflow id:
temporal workflow signal --workflow-id <workflow-id> --name register_mcp_server \
    --input '"RegistryService"' --input '"mcp-registry-endpoint"'
temporal workflow signal --workflow-id <workflow-id> --name register_mcp_server \
    --input '"demo-nexus"' --input '"nexus-hello-demo-endpoint"'
```

(`just setup-nexus`'s namespace/endpoint creation is one-shot infra setup — see the justfile for
the raw `temporal operator ...` commands if running without `just`.)

## If the UI doesn't show "Nexus Hello"

The web UI's `SessionManagerWorkflow` is a singleton set once from whichever `agents.toml`
first started it — restarting `just server` doesn't refresh it. Terminate and let a fresh one
start:

```sh
temporal workflow terminate --workflow-id session-manager
```

## Known gap this example works around

`tool_server.py` declares `@mcp.tool(structured_output=False)`. Without it, FastMCP
auto-generates an `outputSchema`, and `nexus_mcp`'s external-tool round trip currently drops
`structuredContent` on the way back — which fails the MCP client's "declared an outputSchema
but got no structuredContent" validation. This is a gap in `nexus_mcp` itself, not this example;
it'll bite any external MCP server whose tools return structured content. The Nexus-native tool
isn't affected (its tool dicts only ever set `inputSchema`).
