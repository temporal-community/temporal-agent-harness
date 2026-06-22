# Monty travel-agent example

A Temporal-native agent example built on the harness. A "Monty" agent runs a sandboxed Python
script ([pydantic-monty](https://pypi.org/project/pydantic-monty/)) whose only escape hatches are
host functions backed by durable Temporal activities (simulated flight/hotel search + booking).
Three variants ship here:

- **MontyDynamicAgent** — no model in the loop; each turn *is* a pre-written script that the
  workflow runs in the sandbox.
- **MontyChatAgent** — conversational: you chat in plain text, a model writes its own script and
  runs it via a tool, then replies in prose.
- **MontyChatSubagentAgent** — same conversational experience, but it drives `MontyDynamicAgent`
  as a *subagent* (the first end-to-end exercise of the harness subagent toolset).

The agents are driven through the shared session-manager launcher (`examples/session_manager`)
and the shared Svelte UI (`ui`). Their recipes are imported here, so the whole stack runs from
this directory.

## Setup

Copy the env template and fill it in (it's gitignored, so your secrets stay local):

```bash
cp .env.example .env.local
```

- Set `GEMINI_API_KEY` — the conversational agents need it.
- `TEMPORAL_CONFIG_FILE` defaults to the repo's committed `temporal.local.toml` (a local dev
  server). To run against your own server or Temporal Cloud, create a private `temporal.toml`
  (gitignored) and point `TEMPORAL_CONFIG_FILE` at it (see `.env.example`).

## Run

Each command in its own terminal, all from this directory:

```bash
just temporal          # 1. local Temporal dev server (skip if you bring your own)
just session-manager   # 2. session-manager worker
just server            # 3. FastAPI API + built Svelte UI  ->  http://localhost:8000
just worker            # 4. the Monty agents
```

Then open <http://localhost:8000> and pick a Monty agent. For frontend
development, `just ui-dev` runs Vite at <http://127.0.0.1:5173> with `/api`
proxied to the same FastAPI server.

`just` lists every recipe; `just config` shows the Temporal connection currently in use.
