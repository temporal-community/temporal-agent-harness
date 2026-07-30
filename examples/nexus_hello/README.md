# Nexus-transport hello-world agent

Demonstrates `nexus_transport_mcp_server`: a two-tool OpenAI Agents SDK agent whose tools live
entirely outside the workflow, reached over Nexus. `workflow.py`'s `Agent(...)` never mentions
Nexus at all -- `worker.py`'s `OpenAIAgentsPlugin(nexus_mcp_initial_servers={...})` is the only
Nexus-related line in the whole example, and it's the one place both demo tools' names/
endpoints are configured (not split across worker.py and workflow.py).

Demo tools are:
- `demo_get_fun_fact` - a 3rd-party (non-Nexus) MCP server, reached through the **Durable Tools
  Gateway**. `WorkflowTransport` doesn't know it directly, so it falls back to the gateway's
  `RegistryService.call_tool`, which starts `ToolCallWorkflow` on the caller's behalf.
- `demo-nexus_get_lucky_number` - a **Nexus-native** MCP server, called directly via
  `workflow.create_nexus_client()` - no gateway, no activity.

Both are known upfront via `nexus_mcp_initial_servers={...}` - no registration step needed to
use them. `WorkflowTransport` tells a direct server from a gateway/proxy apart structurally,
from what each one's `list_tools` returns - nothing to declare at registration time.

For anything registered LATER, live/self-serve still works: a `register_mcp_server` signal
against the agent's own `NexusMcpServerRegistry` (see the optional `register-gateway` /
`register-nexus-tool` recipes below). It's a signal, not an update: it's buffered by the
server until this workflow is ready to process it, so it works no matter how soon after
starting the conversation you send it - at the cost of no synchronous ack; use the
`list_registered_mcp_servers` query to confirm a registration actually landed.

Three Temporal namespaces, so Nexus is brokering real namespace boundaries:

| Namespace | Hosts |
|---|---|
| `default` | The agent (`worker.py`), session-manager, FastAPI/UI. |
| `gateway` | The Durable Tools Gateway. |
| `nexus-mcp-server` | The demo Nexus-native tool service. |

## Layout

| File | Role |
|---|---|
| `workflow.py` | `NexusHelloAgentWorkflow` - one `ask` handler; `Agent(...)` mentions Nexus nowhere. |
| `worker.py` | Worker on `default`. `OpenAIAgentsPlugin(nexus_mcp_initial_servers={...})` is the only Nexus-related line, and configures both demo tools. |
| `tool_server.py` | Demo 3rd-party MCP server (`get_fun_fact`), registered with the gateway. |
| `nexus_tool_service.py` | Demo Nexus-native MCP server (`get_lucky_number`), built on `authoring.MCPOverNexusServiceHandler`. |
| `agents.toml` | Registry entry for the shared web UI. |

## Run it

Prereqs:
- From the repo root, `cp .env.example .env.local` and set `OPENAI_API_KEY`.
- `nexus-mcp` extra needs Python >=3.13 (`uv sync --extra nexus-mcp`, or just `uv sync` on 3.13+).
- `git` and `go` - `just temporal`/`register-tool` build a `temporal` CLI from source
  automatically (`just cli-build`, cached after the first run) - only needed for the optional
  `register-tool` recipe below, not for the walkthrough itself.

Each in its own terminal, in order:

```sh
just temporal             # 1. local Temporal dev server
just setup-nexus          # 2. ONE-SHOT: 3 namespaces + 2 Nexus endpoints
just tool-server          # 3. demo 3rd-party MCP tool server
just registry             # 4. durable tools gateway -- auto-registers "demo" on startup
just nexus-tool-service   # 5. demo Nexus-native tool service
just session-manager      # 6. session-manager worker
just server               # 7. builds UI, serves API + UI on :8000
just worker               # 8. this agent's worker
```

(`tool-server` before `registry`: the gateway's startup seed fetches from it directly. `just
registry` prints `Seeded external server 'demo' -> ...` once that lands.)

Open http://localhost:8000, pick **Nexus Hello**, start a chat. Both tools work immediately -
no registration step needed, either tool, this or any future session.

```
demo_get_fun_fact:          default -> RegistryService (gateway) -> ToolCallWorkflow -> tool_server.py (HTTP)
demo-nexus_get_lucky_number: default -> nexus_tool_service.py (nexus-mcp-server namespace)
```

**Optional** - registering something else, live, mid-conversation, no restart (see
`register-tool` / `register-gateway` / `register-nexus-tool` in the justfile for the
underlying commands and what each demonstrates).

Without `just`:

```sh
uv run --extra nexus-mcp python -m examples.nexus_hello.tool_server
GATEWAY_SEED_EXTERNAL_SERVERS='{"demo": "http://127.0.0.1:8765/mcp"}' \
    TEMPORAL_NAMESPACE=gateway uv run --extra nexus-mcp --group examples python -m durable_tools_gateway.worker
TEMPORAL_NAMESPACE=nexus-mcp-server uv run --extra nexus-mcp python -m examples.nexus_hello.nexus_tool_service
uv run --group examples python -m examples.session_manager_worker
uv run --group examples python -m examples.app examples/nexus_hello/agents.toml --host 0.0.0.0 --port 8000
uv run --extra nexus-mcp --group examples python -m examples.nexus_hello.worker
```

(`just setup-nexus`'s namespace/endpoint creation is one-shot infra setup - see the justfile for
the raw `temporal operator ...` commands if running without `just`.)

## If the UI doesn't show "Nexus Hello"

The web UI's `SessionManagerWorkflow` is a singleton set once from whichever `agents.toml`
first started it - restarting `just server` doesn't refresh it. Terminate and let a fresh one
start:

```sh
temporal workflow terminate --workflow-id session-manager
```
