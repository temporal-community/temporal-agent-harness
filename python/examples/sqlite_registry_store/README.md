# SQLite registry store

A reference implementation of the `AgentKitRegistryStore` Nexus service: the Agent Kit server's
agent registry kept in one row of a SQLite database, instead of in the registry workflow the
server runs by default. Use it to try a pluggable registry store locally, and as the shape of a
real one: a store holds one JSON document under a version and replaces it only from the version
the writer read. The server applies the registry's rules; the store only compares versions.

The contract is [`contract/registry-store.nexusrpc.yaml`](../../../contract/registry-store.nexusrpc.yaml),
with its bodies in [`contract/registry-store.schema.json`](../../../contract/registry-store.schema.json)
and described in [`contract/README.md`](../../../contract/README.md#the-registry-store-contract). A
store can be written in any Temporal SDK; this one is [`store.py`](store.py).

This is not a production store: one file, on the host its worker runs on.

## Run it against a local dev server

From the repo root, one command per terminal:

```bash
# 1. A dev server that serves standalone Nexus operations, which the Agent Kit server calls the
#    store with. `just temporal` passes the flag.
just temporal

# 2. A Nexus endpoint that routes AgentKitRegistryStore calls to the store's worker.
temporal operator nexus endpoint create \
  --name agent-kit-registry \
  --target-namespace default \
  --target-task-queue agent-kit-registry-store

# 3. The store's worker, keeping the registry in agent-registry.sqlite3.
bazel run //python/examples/sqlite_registry_store:worker -- --db "$PWD/agent-registry.sqlite3"

# 4. The Agent Kit server, keeping its registry behind that endpoint.
bazel run //python/temporal_agent_kit/web:cli -- serve \
  --registry-store-endpoint agent-kit-registry \
  "$PWD/python/examples/monty/agents.toml"
```

Then add, retire and delete agents in the console at <http://localhost:8000> or with
`temporal-agent-kit agents`, and look at what the store holds:

```bash
sqlite3 agent-registry.sqlite3 'SELECT version, snapshot FROM agent_registry'
```

Restart the server without the `agents.toml` to see it serve the registry from the store alone.
Each `read` and `write` is a standalone Nexus operation in the namespace, which the Temporal Web UI
at <http://localhost:8233> can show.

The worker connects like the examples' workers (a `temporal.toml` profile and `TEMPORAL_*`
variables), and takes `--task-queue` to poll another queue than `agent-kit-registry-store`.
