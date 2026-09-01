# Nexus-hello agent

This example shows two things reached over Nexus: a tool, and a subagent. Both use the
same two paths. A **native** path calls the resource directly. A **gateway** path calls a
3rd-party resource through the Durable Tools Gateway. The same gateway brokers both kinds.

The model decides when to use each resource. It never gets called directly by the
workflow. The two MCP tools are plain `Agent(mcp_servers=[...])` entries. The two
subagents are bridged into plain `Agent(tools=[...])` function tools, via
`harness_tool_as_openai_tool`.

- `nexus_native_mcp_server(name, endpoint)` / `agent.nexus_native_subagent(cls, endpoint, key=...)`
  -- one hard-coded native Nexus service, called directly. No registry. No registration.
- `nexus_gateway(account_id).mcp_servers(...)` /
  `agent.nexus_subagent_gateway(account_id).subagent([...], alias, key=...)`
  -- a resource registered ahead of time with the Durable Tools Gateway, called through it.
  The explicit `account_id` selects the account-owned registry.

Native and 3rd-party resources never mix inside the gateway's routing. MCP tools and
subagents share one gateway and one registry workflow. Each HTTP operation uses a
standalone activity.
Tool lists are never cached. They are fetched live from the real MCP server on every
`list_tools()` call. The subagents run no real model. Both give a canned reply. Calling
them proves the transport works, not that they can hold a conversation. But the model
decides whether to call them at all, the same way it decides for the MCP tools.

Resources (registered under account_id `"NexusHelloAccount"`):
- `demo_get_fun_fact` - a 3rd-party (non-Nexus) MCP server, reached through the
  **Durable Tools Gateway** ("demo" -> `http://127.0.0.1:8765/mcp`).
- `demo-nexus_get_lucky_number` - a **native** MCP server, called directly. No gateway.
  No registration.
- `research` - a **native** SUBAGENT (a real harness agent), called directly. No
  gateway. No registration.
- `writer` - a **3rd-party** SUBAGENT factory (plain HTTP, no Nexus, no Temporal client),
  reached through the same **Durable Tools Gateway** as `demo_get_fun_fact`.

## Architecture

```
                            default namespace
                  ┌────────────────────────────────────┐
                  │            NexusHelloAgent          │
                  │       (orchestrator, worker.py)      │
                  │  ask(): model decides whether to use │
                  │  any of the 2 MCP tools / 2 subagents│
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
                   ▼                      ▼                         ▼
        demo-nexus_get_lucky_        (the agent's own       standalone activities
        number (tool)                canned reply)          for MCP and subagents
                                                                  │
                                                          ┌───────┴────────┐
                                                          ▼                ▼
                                                   tool_server.py  subagent_server.py
                                                   (3rd-party MCP) (subagent factory)
```

## How it works

```python
account_gateway = nexus_gateway("NexusHelloAccount")
mcp_servers=[
    account_gateway.mcp_servers("demo"),
    nexus_native_mcp_server("demo-nexus", "nexus-hello-demo-endpoint"),
]

research = agent.nexus_native_subagent(
    NativeResearchSubagentWorkflow, "nexus-hello-subagent-endpoint", key="research"
)
subagent_gateway = agent.nexus_subagent_gateway("NexusHelloAccount")
writer = subagent_gateway.subagent(
    [agent.declared_handler("ask", "...", TextMessage, TextReply, param_name="message")],
    "writer",
    key="writer",
)

sdk_agent = OpenAIAgent(
    ...,
    mcp_servers=[nexus_gateway.mcp_servers("demo"), nexus_native_mcp_server(...)],
    tools=[harness_tool_as_openai_tool(fn) for fn in [*research, *writer]],
)
Runner.run_streamed(sdk_agent, input=..., context=self._runner)  # required: run_tool lives on it
```

`harness_tool_as_openai_tool` turns each subagent function into a plain OpenAI-agents
tool. It dispatches through `AgentWorkflowRunner.run_tool`. Approval gating and
tool-lifecycle events fire the same way they do for any other harness tool. The model
sees the subagent tools the same way it sees the MCP tools. The model decides when to
start a subagent, ask it something, and stop it. The workflow never calls a subagent
directly.

There are two paths to a resource over Nexus: **native**, and **gateway-brokered**. This
example uses both paths, for two resource kinds: MCP tools, and subagents.

### MCP tools

**1. Native tool** (`demo-nexus_get_lucky_number`). The tool server is itself a Nexus
operation handler. There is no gateway and no registry. The agent already knows the
service name and endpoint, from `nexus_native_mcp_server(...)`. Listing tools and calling
a tool are each one Nexus call.

```
Native tool -- demo-nexus_get_lucky_number
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

**2. Gateway tool** (`demo_get_fun_fact`). The real MCP server only speaks HTTP. The
gateway holds its URL. The gateway calls it on the agent's behalf. `.mcp_servers("demo")`
asks the gateway for that alias's tools, once per turn, in one Nexus call. An alias that
isn't registered is skipped silently for now (this is a prototype). `just
register-third-party-mcp-server` registers the URL and then checks it. The gateway fetches
the real tool list on every discovery call. Discovery retries up to three times and then
skips an unavailable server. This needs the dynamic config `just temporal` sets:
`nexusoperation.enableStandalone` and `activity.enableStandalone`.

```
Gateway tool -- demo_get_fun_fact

┌───────┐
│ Agent │
└───┬───┘
    │  Nexus: RegistryService.ListAccountEntries(account_id)     -- once per turn
    │  Nexus: RegistryService.CallTool(account_id, alias, name, args)
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

**3. Traditional MCP** (not used by this demo -- shown for contrast). Plain HTTP,
wrapped in an activity. This is what `stateless_mcp_server`/`MCPServerStreamableHttp`
give you.

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

### Subagents

Subagents use the same two paths as MCP tools. The resource is a whole subagent, not a
tool.

**1. Native subagent** (`research`). Same shape as the native tool above, but the
resource is a whole harness agent (`native_subagent.py`), not a tool. `sendAgentMessage`
sends one turn. `pollMessages` uses the requested long-poll timeout. Both are Nexus
operations, called directly from workflow code. There is no
gateway and no standalone activity. The cost is the same as a native MCP tool. This is
the same `AgentService` contract that fronts the Slack connector. Here it runs in the
same worker as the agent workflow it fronts -- one process, one task queue. The Slack
connector instead runs the two as separate processes, to scale Nexus traffic on its own.
Both are valid (`nexus_agent_adapter/worker.py`'s own docstring documents same-worker as
an explicit alternative). This demo picks the simpler one.

```
Native subagent -- research
┌───────┐
│ Agent │
└───┬───┘
    │  Nexus: AgentService.sendAgentMessage(session_id, "ask", payload)
    │  Nexus: AgentService.pollMessages(session_id, cursor)  -- bounded long-poll
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

**2. Gateway subagent** (`writer`). The registered alias identifies an HTTP factory.
`startSubagent` creates an instance and returns a gateway-owned ID. Turn and stop calls
use only that ID, so a factory registration change cannot redirect a running instance.
Two instances of `writer` have separate state. Start and turn requests use
idempotency keys. Their activities can retry up to five times. The MCP tool-call activity
does not retry because an MCP tool may have side effects.

```
Gateway subagent -- writer
┌───────┐
│ Agent │
└───┬───┘
    │  Nexus: RegistryService.startSubagent(account_id, alias) -> instance_id
    │  Nexus: RegistryService.dispatchSubagentTurn(account_id, instance_id, expected_turn, ...)
    │  Nexus: RegistryService.stopSubagent(account_id, instance_id)
    ▼
┌───────────────────────────┐
│  Nexus Operation Handler  │   the SAME "Durable Tools Gateway" as above --
│  (durable_tools_gateway)  │   extended with a "subagent" resource kind alongside "mcp_tool"
└─────────────┬─────────────┘
              │  standalone activities: start, turn, stop
              │  turn key = account_id:instance_id:expected_turn
              ▼
       ┌──────────────────────┐
       │  subagent factory    │
       │  subagent_server.py  │   HTTP: /sessions/{instance_id}/...
       └──────────────────────┘
```

### One gateway, two resource kinds

The Durable Tools Gateway is one Nexus service in one namespace. One registry workflow
stores MCP server URLs and subagent factory URLs. The HTTP operations use separate
activities and retry rules.

```
                        gateway namespace
              ┌───────────────────────────────────────┐
              │            RegistryService              │
              │         (durable_tools_gateway)          │
              │                                          │
              │  ToolRegistryWorkflow -- one registry     │
              │  one entry per resource:                  │
              │    account_id + alias -> kind + url         │
              │    kind = mcp_tool | subagent             │
              └────────────────┬─────────────────────────┘
                               │
                  ┌────────────┴──────────────┐
                  ▼                           ▼
          MCP activities              subagent activities
          list: 3 attempts             start: 5 attempts
          call: 1 attempt              turn: 5 attempts
                                       stop: 5 attempts
                  │                           │
                  ▼                           ▼
          tool_server.py               subagent_server.py
          (3rd-party MCP)              (subagent factory)
```

Four Temporal namespaces show cross-namespace Nexus calls:

| Namespace | Hosts |
|---|---|
| `default` | The agent (`worker.py`), session-manager, FastAPI/UI. |
| `gateway` | The Durable Tools Gateway. Brokers both `demo` (tool) and `writer` (subagent). |
| `nexus-mcp-server` | The demo native tool service. |
| `nexus-subagent-server` | The demo native subagent's own agent workflow. |

## Demo limits

- The HTTP factory stores sessions and idempotency keys in memory. A production provider
  must store them durably.
- A graceful parent close stops its active instances. A forced workflow termination cannot
  run cleanup. A production provider should also expire inactive instances.

Three different IDs are in play here. `agents.toml`'s key (`"nexus-hello"`) identifies
the UI entry, `NexusHelloAgent` is the Temporal workflow type, and
`NexusHelloAccount` owns the gateway resources. They are deliberately independent.

## Layout

| File | Role |
|---|---|
| `workflow.py` | `NexusHelloAgentWorkflow` - `ask` handler: model decides whether to use any of `mcp_servers=[...]` or the two bridged subagent tools (`tools=[...]`). |
| `worker.py` | Worker on `default`. No Nexus-related plugin config. |
| `tool_server.py` | Demo 3rd-party MCP server (`get_fun_fact`). |
| `nexus_tool_service.py` | Demo native MCP server (`get_lucky_number`), built on `authoring.MCPOverNexusServiceHandler`. |
| `native_subagent.py` | Demo native SUBAGENT (`NativeResearchSubagentWorkflow`) + its worker entrypoint -- one worker running the workflow AND its `AgentServiceHandler` Nexus front door. |
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
just nexus-tool-service               # 6. demo native tool service
just nexus-subagent                  # 7. demo native subagent -- agent workflow AND
                                      #    its Nexus front door, one worker
just register-third-party-mcp-server # 8. ONE-SHOT: registers "demo" under account_id
                                      #    "NexusHelloAccount"
just register-third-party-subagent   # 9. ONE-SHOT: registers "writer" under the same account_id
just session-manager                 # 10. session-manager worker
just server                          # 11. builds UI, serves API + UI on :8000
just worker                          # 12. this example's agent worker
```

Open http://localhost:8000, pick **Nexus Hello**, start a chat. Ask something that calls
for research and writing (e.g. "research X and write a short summary") and the model
will use both subagents alongside the two MCP tools. All four resources work
immediately. The model decides whether each one is relevant to what you asked.

```
(every turn)                 default -> RegistryService.list_account_entries("NexusHelloAccount")  (gateway tool only)
demo_get_fun_fact:            default -> RegistryService (gateway) -> mcp_proxy_activity (standalone activity) -> tool_server.py (HTTP)
demo-nexus_get_lucky_number:  default -> nexus_tool_service.py (nexus-mcp-server namespace), no gateway hop
research (subagent):          default -> AgentServiceHandler (nexus-subagent-server namespace), no gateway hop
writer (subagent):             default -> RegistryService (gateway) -> start/turn/stop activities -> subagent_server.py (HTTP)
```

Without `just` (from the repo root):

```sh
uv run --extra nexus-mcp python -m examples.nexus_hello.tool_server
uv run --extra nexus-mcp --group examples python -m examples.nexus_hello.subagent_server
TEMPORAL_NAMESPACE=gateway GATEWAY_SEED_ACCOUNT_ID=NexusHelloAccount uv run --extra nexus-mcp --group examples python -m durable_tools_gateway.worker
TEMPORAL_NAMESPACE=nexus-mcp-server uv run --extra nexus-mcp python -m examples.nexus_hello.nexus_tool_service
TEMPORAL_NAMESPACE=nexus-subagent-server uv run --group examples python -m examples.nexus_hello.native_subagent worker
temporal workflow signal --namespace gateway \
    --workflow-id account-registry-cf700a56bafc7c6f1417b0fda1135aedd0298c6266fb173b325d69db81b09a8f \
    --name register_external --input '"demo"' --input '"http://127.0.0.1:8765/mcp"'
temporal workflow signal --namespace gateway \
    --workflow-id account-registry-cf700a56bafc7c6f1417b0fda1135aedd0298c6266fb173b325d69db81b09a8f \
    --name register_subagent --input '"writer"' --input '"http://127.0.0.1:8766"'
uv run --group examples python -m examples.session_manager_worker
uv run --group examples python -m examples.app examples/nexus_hello/agents.toml --host 0.0.0.0 --port 8000
uv run --extra nexus-mcp --group examples python -m examples.nexus_hello.worker
```

(`just setup-nexus`'s namespace/endpoint creation is one-shot infra setup - see the justfile
for the raw `temporal operator ...` commands if running without `just`.)

## If the UI doesn't show "Nexus Hello"

The web UI's `SessionManagerWorkflow` is a singleton. It is set once, from whichever
`agents.toml` first started it. Restarting `just server` does not refresh it. Terminate it
and let a fresh one start:

```sh
temporal workflow terminate --workflow-id session-manager
```
