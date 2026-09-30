# Sano Hello: the web UI over HTTP and standalone Nexus

A hello-world OpenAI Agents SDK agent with two activity tools (`get_weather`,
`get_local_time`). The packaged web UI reaches it without the FastAPI server:

```
browser -> HTTP -> standalone Nexus operation (connector namespace) -> AgentService -> agent
```

- `cmd/web` (in `nexus/ui_connector`) serves the UI and the
  [inbound HTTP connector](https://github.com/bergundy/nexus-inbound-http-connector). It maps
  this agent to `nexus_endpoint` in `agents.toml`.
- The worker hosts the agent workflow, the tool activities, and the `AgentService` Nexus
  handler, all on the `sano-hello` task queue. The endpoint targets that queue.
- See `nexus/ui_connector/README.md` for the flow diagrams and the limits.

## Layout

| File | Role |
|---|---|
| `workflow.py` | The agent: one `ask` handler that runs the OpenAI Agents SDK |
| `tools.py` | The two activity tools (canned results, no network) |
| `worker.py` | Agent workflow + tool activities + `AgentService` on one worker |
| `agents.toml` | The agent entry, with `nexus_endpoint` |
| `justfile` | The local stack |

## Run it

First-time setup: `cp .env.example .env.local` from the repo root and fill in
`OPENAI_API_KEY`. You also need Go 1.26+ and the `temporal` CLI 1.9.1+.

From this directory, each command in its own terminal:

```bash
just temporal        # local Temporal dev server with the Nexus settings
just setup-nexus     # once: the "connector" namespace + sano-hello-agent-endpoint
just worker          # agent workflow, tool activities, AgentService
just web-connector   # http://localhost:8080
```

Open http://localhost:8080 and ask, for example, "What's the weather and the time in Paris?"

If every call times out after 10 s, the worker is not running.
