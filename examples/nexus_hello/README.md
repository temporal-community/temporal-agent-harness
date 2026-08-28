# Nexus-hello agent

Demonstrates two ways to reach a resource over Nexus, side by side in one
`Agent(mcp_servers=[...])` plus two direct subagent calls -- for **two different resource
kinds** (a tool, and a whole subagent), proving the same gateway and the same native-Nexus
path both generalize beyond MCP:

- `nexus_native_mcp_server(name, endpoint)` / `agent.nexus_native_subagent(cls, endpoint, key=...)`
  -- one hard-coded native Nexus service, called directly. No registry, no registration, ever.
- `nexus_tools_gateway().mcp_servers(...)` / `agent.nexus_subagent_gateway().subagent([...], alias, key=...)`
  -- an explicit resource registered ahead of time with the Durable Tools Gateway, proxied
  through it. `agent_id` is inferred from this workflow's own type (`workflow_type`), not
  chosen by hand.

Native and 3rd-party resources never mix inside the gateway's routing -- but MCP tools and
subagents now share the same gateway deployable, same registry workflow, different proxy
activity. Tool lists are never cached: they're fetched live, from the real MCP server, on
every `list_tools()` call. The subagents run no real model -- both give canned replies, so
that half of the demo needs no API key; only the MCP-tool half talks to a live model.

Resources (registered under agent_id `"NexusHelloAgent"`, this workflow's `workflow_type`):
- `demo_get_fun_fact` - a 3rd-party (non-Nexus) MCP server, reached through the
  **Durable Tools Gateway** ("demo" -> `http://127.0.0.1:8765/mcp`).
- `demo-nexus_get_lucky_number` - a **Nexus-native** MCP server, called directly - no
  gateway, no registration.
- `research` - a **Nexus-native** SUBAGENT (a real harness agent), called directly - no
  gateway, no registration.
- `writer` - a **3rd-party** SUBAGENT (plain HTTP, no Nexus, no Temporal client of its
  own), reached through the SAME **Durable Tools Gateway** as `demo_get_fun_fact`.

## Architecture

```
                            default namespace
                  ┌────────────────────────────────────┐
                  │            NexusHelloAgent          │
                  │       (orchestrator, worker.py)      │
                  │  ask(): model calls the 2 MCP tools; │
                  │  then drives the 2 subagents directly│
                  └───┬────────────────┬──────────────┬──┘
                      │                │              │
          native tool │    native subagent│   gateway (both kinds)
                      ▼                ▼              ▼
        ┌────────────────────┐┌───────────────────────┐┌──────────────────────┐
        │ nexus-mcp-server ns││ nexus-subagent-server  ││   gateway namespace   │
        │                     ││        ns              ││                       │
        │ nexus_tool_         ││  ONE worker: agent     ││   RegistryService     │
        │ service.py          ││  workflow + its Nexus  ││  (durable_tools_      │
        │ (Nexus service      ││  front door together   ││   gateway)            │
        │  handler only,       ││  (native_subagent.py)  ││  kind: mcp_tool       │
        │  no workflow --      ││  AgentServiceHandler   ││       | subagent      │
        │  a tool has no       ││  -> NativeResearchSub- ││                       │
        │  backing agent)      ││  agentWorkflow          ││                       │
        └──────────┬──────────┘└──────────┬────────────┘└───────────┬───────────┘
                   ▼                      ▼                standalone activity,
        demo-nexus_get_lucky_        (the agent's own            one per kind
        number (tool)                canned reply)             ┌──────┴──────┐
                                                                 ▼             ▼
                                                       mcp_proxy_    subagent_proxy_
                                                       activity       activity
                                                            │              │
                                                            ▼              ▼
                                                    tool_server.py  subagent_server.py
                                                    (3rd-party MCP) (3rd-party subagent)
```

## How it works

```python
nexus_gateway = nexus_tools_gateway()  # agent_id inferred from workflow_type
mcp_servers=[
    nexus_gateway.mcp_servers("demo"),
    nexus_native_mcp_server("demo-nexus", "nexus-hello-demo-endpoint"),
]

research = agent.nexus_native_subagent(
    NativeResearchSubagentWorkflow, "nexus-hello-subagent-endpoint", key="research"
)
subagent_gateway = agent.nexus_subagent_gateway()  # agent_id inferred from workflow_type
writer = subagent_gateway.subagent(
    [agent.declared_handler("ask", "...", TextMessage, TextReply, param_name="message")],
    "writer",
    key="writer",
)
```

Four ways to reach a resource over Nexus. This demo uses all four.

**1. Nexus-native tool** (`demo-nexus_get_lucky_number`). The tool server IS a Nexus
operation handler. No gateway, no registry, no discovery call -- the agent always
knows `name` + `endpoint` statically, from `nexus_native_mcp_server(...)` itself. Both
listing and calling are one direct Nexus hop each.

```
Nexus-native tool -- demo-nexus_get_lucky_number
("MCPServerStdio"-shaped, but the transport is Nexus, not a process or HTTP)

┌───────┐
│ Agent │
└───┬───┘
    │  Nexus: demo-nexus.ListTools
    │  Nexus: demo-nexus.GetLuckyNumber(topic)
    ▼
┌───────────────────────────┐
│  Nexus Operation Handler  │
│  (nexus_tool_service.py)  │
└───────────────────────────┘
```

**2. Gateway-proxied tool** (`demo_get_fun_fact`). The real MCP server only speaks
HTTP. The gateway holds the URL and dispatches for it, so the agent never has to.
`.mcp_servers("demo")` asks the gateway for exactly those aliases' tools, once per
turn, in a single Nexus call -- an alias that isn't actually registered is silently
skipped (no error yet; this is a prototype). `just register-third-party-mcp-server` (`temporal
workflow signal`) only validates the URL — the actual tool list is fetched live at
discovery time, every turn, as a standalone activity (Nexus + SAA). Needs the dynamic
config `just temporal` sets: `nexusoperation.enableStandalone`/`activity.enableStandalone`.

```
Gateway-proxied tool -- demo_get_fun_fact

┌───────┐
│ Agent │
└───┬───┘
    │  Nexus: RegistryService.ListAgentEntries(agent_id)     -- once per turn
    │  Nexus: RegistryService.CallTool(agent_id, alias, name, args)
    ▼
┌───────────────────────────┐
│  Nexus Operation Handler  │   "Durable Tools Gateway" --
│  (durable_tools_gateway)  │   isolates auth/creds from the agent
└─────────────┬─────────────┘
              │  standalone activity: mcp_proxy_activity
              │  (Nexus + SAA -- no backing per-call workflow)
              ▼
       ┌──────────────────┐
       │    MCP server    │
       │  tool_server.py  │   HTTP
       └──────────────────┘
```

**3. Nexus-native subagent** (`research`). Same shape as #1, but the resource is a whole
harness agent (`native_subagent.py`), not a tool. `sendAgentMessage` (dispatch) and
`pollMessages` (the reply, a bounded long-poll, looped) are two Nexus operations awaited
directly from workflow code -- no gateway, no standalone activity, same cost profile as a
native MCP tool. It's the same `AgentService` contract that fronts the Slack connector --
but here it runs in the SAME worker as the agent workflow it fronts (one process, one task
queue), rather than as a separate front-door process the way the Slack connector deploys
it. Both are valid (`nexus_agent_adapter/worker.py`'s own docstring documents same-worker
as an explicit alternative); this demo picks the simpler one.

```
Nexus-native subagent -- research
┌───────┐
│ Agent │
└───┬───┘
    │  Nexus: AgentService.sendAgentMessage(session_id, "ask", payload)
    │  Nexus: AgentService.pollMessages(session_id, cursor)  -- bounded long-poll, looped
    │  Nexus: AgentService.closeSession(session_id)          -- on stop
    ▼
┌─────────────────────────────────────────┐
│      nexus-subagent-server worker       │
│  ┌────────────────────────────────────┐ │
│  │         AgentServiceHandler        │ │   the SAME contract fronting
│  │        (nexus_agent_adapter)       │ │   the Slack connector
│  └──────────────────┬─────────────────┘ │
│                      ▼                  │
│    NativeResearchSubagentWorkflow       │
│         (native_subagent.py)            │
└─────────────────────────────────────────┘
```

**4. Gateway-brokered subagent** (`writer`). Same shape as #2, but the resource is a whole
subagent that only speaks plain HTTP, not an MCP tool. `dispatch_subagent_turn` proxies
each turn as a standalone activity (Nexus + SAA) -- the SAME gateway deployable as #2, a
different resource kind (`subagent`, not `mcp_tool`), same registry, a different proxy
activity. Unlike the MCP proxy's tool calls (arbitrary, no idempotency contract), a
subagent turn carries a caller-known idempotency key (`agent_id:alias:expected_turn`), so
retries are enabled here instead of forbidden.

```
Gateway-brokered subagent -- writer
┌───────┐
│ Agent │
└───┬───┘
    │  Nexus: RegistryService.dispatchSubagentTurn(agent_id, alias, msg_type, payload, expected_turn)
    │  Nexus: RegistryService.stopSubagent(agent_id, alias)   -- on stop
    ▼
┌───────────────────────────┐
│  Nexus Operation Handler  │   the SAME "Durable Tools Gateway" as #2 --
│  (durable_tools_gateway)  │   extended with a "subagent" resource kind alongside "mcp_tool"
└─────────────┬─────────────┘
              │  standalone activity: subagent_proxy_activity
              │  (Nexus + SAA -- retries enabled, idempotency key = agent_id:alias:turn)
              ▼
       ┌──────────────────────┐
       │  3rd-party subagent  │
       │  subagent_server.py  │   HTTP: POST /turns, POST /close
       └──────────────────────┘
```

**5. Traditional MCP with Temporal plugin** (not used by this demo — shown for contrast). HTTP wrapped in activities.
This is what `stateless_mcp_server`/`MCPServerStreamableHttp` give you.

```
Traditional MCP -- for contrast, not used by this demo

┌───────┐
│ Agent │
└───┬───┘
    │  HTTP (wrapped in activity): ListTools()
    │  HTTP (wrapped in activity): CallTool(name, args)
    ▼
┌────────────────────────────┐
│         MCP server         │
│  served at http://.../mcp  │
└────────────────────────────┘
```

Four Temporal namespaces to demonstrate cross-namespace Nexus calls:

| Namespace | Hosts |
|---|---|
| `default` | The agent (`worker.py`), session-manager, FastAPI/UI. |
| `gateway` | The Durable Tools Gateway -- brokers BOTH `demo` (tool) and `writer` (subagent). |
| `nexus-mcp-server` | The demo Nexus-native tool service. |
| `nexus-subagent-server` | The demo Nexus-native subagent's own agent workflow. |

Note two different identifiers are in play: `agents.toml`'s `key` (`"nexus-hello"`) is
just how the web UI routes to this agent; the gateway's `agent_id` (`"NexusHelloAgent"`)
is this workflow's `workflow_type`, used only for gateway registration/lookup. They
don't have to match, and here they don't.

## Layout

| File | Role |
|---|---|
| `workflow.py` | `NexusHelloAgentWorkflow` - `ask` handler: model uses `mcp_servers=[...]`, then drives both subagents directly via `_ask_subagents`. |
| `worker.py` | Worker on `default`. No Nexus-related plugin config. |
| `tool_server.py` | Demo 3rd-party MCP server (`get_fun_fact`). |
| `nexus_tool_service.py` | Demo Nexus-native MCP server (`get_lucky_number`), built on `authoring.MCPOverNexusServiceHandler`. |
| `native_subagent.py` | Demo Nexus-native SUBAGENT (`NativeResearchSubagentWorkflow`) + its worker entrypoint -- one worker running the workflow AND its `AgentServiceHandler` Nexus front door. |
| `subagent_server.py` | Demo 3rd-party SUBAGENT -- plain FastAPI HTTP server, proxied through the same gateway as `tool_server.py`. |

## Run it

Prereqs:
- From the repo root, `cp .env.example .env.local` and set `OPENAI_API_KEY`.
- `nexus-mcp` extra needs Python >=3.13 (`uv sync --extra nexus-mcp`, or just `uv sync` on 3.13+).
- `temporal` CLI on PATH (the stable public release; no custom build needed).

Each in its own terminal, in order:

```sh
just temporal                        # 1. local Temporal dev server
just setup-nexus                     # 2. ONE-SHOT: 4 namespaces + 3 Nexus endpoints
just third-party-mcp-server          # 3. demo 3rd-party MCP tool server
just third-party-subagent            # 4. demo 3rd-party subagent
just registry                        # 5. durable tools gateway (no seed config -- starts empty)
just nexus-tool-service               # 6. demo Nexus-native tool service
just nexus-subagent                  # 7. demo Nexus-native subagent -- agent workflow AND
                                      #    its Nexus front door, one worker
just register-third-party-mcp-server # 8. ONE-SHOT: registers "demo" under agent_id
                                      #    "NexusHelloAgent"
just register-third-party-subagent   # 9. ONE-SHOT: registers "writer" under the same agent_id
just session-manager                 # 10. session-manager worker
just server                          # 11. builds UI, serves API + UI on :8000
just worker                          # 12. this example's agent worker
```

Open http://localhost:8000, pick **Nexus Hello**, start a chat. All four resources work
immediately.

```
(every turn)                 default -> RegistryService.list_agent_entries("NexusHelloAgent")  (gateway-proxied tool only)
demo_get_fun_fact:            default -> RegistryService (gateway) -> mcp_proxy_activity (standalone activity) -> tool_server.py (HTTP)
demo-nexus_get_lucky_number:  default -> nexus_tool_service.py (nexus-mcp-server namespace), no gateway hop
research (subagent):          default -> AgentServiceHandler (nexus-subagent-server namespace), no gateway hop
writer (subagent):             default -> RegistryService (gateway) -> subagent_proxy_activity (standalone activity) -> subagent_server.py (HTTP)
```

Without `just` (from the repo root):

```sh
uv run --extra nexus-mcp python -m examples.nexus_hello.tool_server
uv run --extra nexus-mcp --group examples python -m examples.nexus_hello.subagent_server
TEMPORAL_NAMESPACE=gateway uv run --extra nexus-mcp --group examples python -m durable_tools_gateway.worker
TEMPORAL_NAMESPACE=nexus-mcp-server uv run --extra nexus-mcp python -m examples.nexus_hello.nexus_tool_service
TEMPORAL_NAMESPACE=nexus-subagent-server uv run --group examples python -m examples.nexus_hello.native_subagent worker
temporal workflow signal --namespace gateway --workflow-id mcp-tool-registry --name register_external \
    --input '"NexusHelloAgent"' --input '"demo"' --input '"http://127.0.0.1:8765/mcp"'
temporal workflow signal --namespace gateway --workflow-id mcp-tool-registry --name register_subagent \
    --input '"NexusHelloAgent"' --input '"writer"' --input '"http://127.0.0.1:8766"'
uv run --group examples python -m examples.session_manager_worker
uv run --group examples python -m examples.app examples/nexus_hello/agents.toml --host 0.0.0.0 --port 8000
uv run --extra nexus-mcp --group examples python -m examples.nexus_hello.worker
```

(`just setup-nexus`'s namespace/endpoint creation is one-shot infra setup - see the justfile
for the raw `temporal operator ...` commands if running without `just`.)

## If the UI doesn't show "Nexus Hello"

The web UI's `SessionManagerWorkflow` is a singleton set once from whichever `agents.toml`
first started it - restarting `just server` doesn't refresh it. Terminate and let a fresh one
start:

```sh
temporal workflow terminate --workflow-id session-manager
```
