# Registering agent types on a running server

> Status: **implemented**. Moves the server's agent registry out of process memory into a pluggable
> registry store, so agent types are added, retired and deleted while the server runs. The store
> the package ships keeps the registry in a Temporal workflow; a deployment can supply its own
> backed by a database.

## The problem

`temporal-agent-kit serve a/agents.toml b/agents.toml` reads its registries once, in the app's
lifespan, and keeps the merged `AgentRegistry` in `app.state` for the life of the process. Adding
an agent type means stopping the server and starting it again over different files.

Since sessions are listed from visibility (`visibility-session-list.md`), that restart costs no
sessions, but it still ties the set of agent types to whoever controls the server's process and
files. A server hosted for a team cannot take a new agent type from the people who wrote it.

The registry is read in three places, and nowhere else:

- `GET /api/agents`, for the console's agent picker and the chat-server's routing.
- `POST /api/sessions`, to find an agent's workflow type and task queue.
- The session list and `GET /api/sessions/{id}`, whose visibility query is an `OR` of each
  agent's `(WorkflowType, TaskQueue)`.

Every route that acts on a session by id (attach, messages, status, approvals, close) ignores it.
So the server only needs to read the registry from somewhere that can change; nothing else moves.

## The design

### One source: the registry store

**The server reads agent types from its registry store and from nowhere else.** The
`agents.toml` files given to `serve` are a way to fill the store (below), not a second source.
The server keeps no copy: every request that needs the registry reads it from the store, so all
replicas agree as soon as a write completes.

The registry is one document:

```python
class RegisteredAgent(BaseModel):
    key: str
    workflow_type: str
    task_queue: str                  # as registered, without TASK_QUEUE_PREFIX
    label: str
    description: str
    schema_document: dict[str, JsonValue] | None  # the agent's whole schema document
    start: AgentStart | None         # its init data, extracted from `schema` at registration
    retired: bool = False


class RegistrySnapshot(BaseModel):
    agents: dict[str, RegisteredAgent] = {}   # by key
```

**It stores what was registered, so it can be given back.** The whole schema document is kept,
not only the init data the server reads, so the console can download an `agents.toml` with its
schema files that registers the same agents again (below), and a client can read an agent's
handlers and states before any session of it exists. `start` is extracted once, when an entry is
registered, so reading the agent list never parses schema documents.

`task_queue` is kept as it was registered, without the prefix. The server puts it under its
`TASK_QUEUE_PREFIX` whenever it reads an entry, as it does when it loads a file today. A store
never sees the prefix, and a downloaded file names the same queues as the file that registered
them.

### The store interface

A store holds the document under a version and replaces it only if the version has not moved:

```python
class VersionedSnapshot(BaseModel):
    snapshot: RegistrySnapshot
    version: int = 0                 # 0 when nothing has been written


class StaleRegistry(Exception):
    """The stored version is no longer the one a write expected."""


class AgentRegistryStore(Protocol):
    async def read(self) -> VersionedSnapshot: ...

    async def write(self, snapshot: RegistrySnapshot, expected: int) -> int:
        """Store ``snapshot`` as version ``expected + 1`` if the stored version is ``expected``,
        and return the new version. Raise :class:`StaleRegistry` otherwise."""
```

That is all a backend implements. Every store has this operation:

- Postgres, SQLite, DuckDB: one row, `UPDATE registry SET doc = ?, version = version + 1 WHERE
  version = ?`, with an `INSERT` for the first write.
- Cassandra: one partition, a lightweight transaction `UPDATE … IF version = ?`.
- The registry workflow the package ships (below): an Update whose validator compares versions.

**The rules live in the server, not the store.** Registering, retiring and deleting are pure
functions from a snapshot and a request to a new snapshot, in one module every store shares. The
server reads, applies the function, and writes with the version it read; on `StaleRegistry` it
reads again and reapplies, a bounded number of times. A change that leaves the snapshot as it was
is not written, so a restart that registers the same files again writes nothing.

The functions reject:

- a key or workflow type used twice in one `register`, or a workflow type another key already
  has;
- a `register` that changes the `workflow_type` or `task_queue` of an existing key. The pair is
  what finds an agent's sessions in visibility, so changing it would hide them. Delete the entry
  first to repoint a key;
- `retire` or `delete` of a key that is not there.

`register` adds or replaces every entry it is given, all or none, and a replaced entry becomes
active again.

The rules span entries, which is why the interface is one versioned document rather than a row
per agent: "no other key has this workflow type" and "a file registers whole or not at all" need
either multi-row transactions with unique constraints, which some stores (Cassandra) lack, or a
compare-and-swap on one document, which every store has. A row-per-agent interface would put the
rules in every backend, in its own transaction model.

### Choosing a store

The registry workflow store is built in and is the default: `serve` with no store options keeps
the registry there. **Any other store is a Nexus service**, `AgentKitRegistryStore`, which the
server reaches through a Nexus endpoint:

```
temporal-agent-kit serve --registry-store-endpoint agent-kit-registry agents.toml
```

| Operation | Input | Output |
| --- | --- | --- |
| `read` | `{}` | `{"snapshot": {...}, "version": n}`; version 0 and an empty registry before anything is written. |
| `write` | `{"snapshot": {...}, "expected": n}` | `{"stored": true, "version": n + 1}` when the stored version was `n`; otherwise `{"stored": false, "version": <stored>}`, storing nothing. |

The service is `contract/registry-store.nexusrpc.yaml`, its bodies' JSON Schema is
`contract/registry-store.schema.json` (`temporal-agent-kit schema --registry-store`), and
`contract/README.md` describes the contract. A store is two synchronous operations over one row,
on a worker in any Temporal SDK, with whatever database the deployment uses; the server never
imports a deployer's code, and no database driver becomes a dependency of the package.
`python/examples/sqlite_registry_store` is a reference store over SQLite.

**Why Nexus.** The server calls the store's operations as standalone Nexus operations with the
Temporal client it already has. Who may call the store is the endpoint's access rules, so the
store needs no credentials of its own, and the store can live in another namespace. An HTTP
service would have needed its own authentication.

**What it costs.** Every registry read is a Nexus operation execution, which the namespace
records. The server reads the registry for each agent list, session list, session lookup and
session start; the console no longer refreshes the session list on a timer, only when asked and
when the reader returns to the window, and the chat-server keeps agent keys rather than listing
agents on every message. Standalone Nexus operations are experimental, and a server serves them
only with the dynamic config value `nexusoperation.enableStandalone=true` (`just temporal` sets
it). Entering the store reads the registry once, so an endpoint that cannot be reached stops
`serve` as it starts; a failed operation is a `502` from the server's registry routes.

In the server, every store is an object with two methods, `read()` and
`write(snapshot, expected)`, built by a factory the server calls with its Temporal client and
enters for its lifespan
(`RegistryStoreFactory = Callable[[Client], AbstractAsyncContextManager[AgentRegistryStore]]`).
`create_agent_kit_app(registry_store=...)` takes the factory, for an embedder and for the tests'
in-memory store; `serve` chooses between the two built-in ones and loads no factory by name.

### The registry workflow store

The default store keeps the document in one workflow, `AgentRegistryWorkflow`, with the id
`agent-kit-registry` on the task queue `agent-kit-registry`. There is one per namespace, or one
per `TASK_QUEUE_PREFIX` within a namespace: the prefix goes on the workflow id as well as the
task queue, so under `TASK_QUEUE_PREFIX=alice` both are `alice-agent-kit-registry`. Servers with
the same prefix in the same namespace, replicas included, share one registry; servers with
different prefixes see only their own, as they see only their own sessions.

The workflow holds a `VersionedSnapshot` and has one Update and one query:

| Handler | Does |
|---|---|
| `write(snapshot, expected)` | Its validator rejects a stale `expected`, which the store raises as `StaleRegistry`; otherwise the snapshot is stored under the next version. |
| `read()` (query) | The `VersionedSnapshot`. |

It knows nothing about agents: the rules run in the server, so the workflow's code is a few lines
that should rarely change.

**Hosting it: a worker inside the server.** Entering the store starts a worker on the registry's
task queue that hosts only `AgentRegistryWorkflow`. Each replica polls the queue; any of them
serves an Update or query. That is a worker, not another process: whenever the API can be called,
a worker exists to answer it. A deployment on another store runs no worker.

Nothing starts the workflow at server startup. Whoever first needs it creates it:

- **Writes are Update-with-Start.** Every `write` is sent with
  `execute_update_with_start_workflow` and a `WithStartWorkflowOperation` using
  `WorkflowIDConflictPolicy.USE_EXISTING` (as `AgentClient` already does for messages). The first
  write creates the registry; every later one, from any replica, reaches the running one.
- **Reads are a plain query.** Temporal has Signal-with-Start and Update-with-Start but no
  Query-with-Start, and a read should not start a workflow or write history anyway. The query is
  sent with `QueryRejectCondition.NOT_OPEN`, and a registry that does not exist (`NOT_FOUND`) or
  is not running reads as an empty snapshot at version 0. A terminated registry therefore reads as
  empty rather than as its last state, which matches what the next write sees: Update-with-Start
  over a closed workflow starts a fresh, empty run.

An Update completes once the workflow has accepted the write, so a client that registers an agent
and then lists agents sees it. Terminating the workflow removes registrations only: sessions have
no parent and keep running.

### Upgrading a server that runs the registry workflow

The registry workflow runs for as long as the deployment does, and its worker is whichever server
release is running. A worker that has not cached the workflow rebuilds it by replaying its history
with its own code, so a release has to be able to replay history the previous release wrote, and
during a rolling deploy the two releases poll the same queue at once.

- **The workflow sends no commands.** No activities, timers, child workflows or memo writes:
  only the Update, the query and continue-as-new. Replay fails when code produces commands history
  does not have, and this code produces none. What a release must keep is how it rebuilds state
  from the history, which for this workflow is "the last accepted snapshot".
- **Its data stays compatible across one release.** `VersionedSnapshot` and the Update's input
  are pydantic models that a release reads in the previous release's shape (new fields have
  defaults) and that ignore fields they do not know, so the previous release reads the new
  shape during a rolling deploy.
- **It continues as new when its code changes.** `REGISTRY_WORKFLOW_REVISION` is an integer
  bumped with any change to the workflow's code or the models it holds, and the workflow's input
  records the revision that started its run. Entering the store sends an
  `ensure_revision(revision)` Update, whose validator rejects it, writing nothing, unless
  `revision` is newer. A newer one makes the workflow continue as new, carrying its snapshot, once
  its handlers finish. A release that leaves the workflow alone causes no continue-as-new, a
  release that changes it causes one, replica restarts cause none, and an old replica cannot move
  the run back. After it, a release replays only history written by its own revision or the one
  before it. The workflow also continues as new when Temporal suggests it.
- **CI replays the previous revision's history.** A history recorded at each revision is checked
  in beside the tests (`registry_workflow_revision_1.history.json`) and replayed with Temporal's
  `Replayer` against the current code, so an incompatible change fails the build, not a deploy.
  Bumping the revision records the next one: `bazel test //python/tests/web:test_registry_workflow
  --test_env=AGENT_KIT_RECORD_REGISTRY_HISTORY=1 --test_filter=test_record_the_current_revisions_history`
  writes it to the test's undeclared outputs.
- **An incompatible change gets a new workflow.** A release that cannot read the previous
  release's data moves to a new id (`agent-kit-registry-v2`). Its agents are registered again
  from a download of the previous release's registry (below) with `agents add`.

**One registry worker per process.** The SDK refuses a second worker on the same task queue in
one process, so a process can enter the registry workflow store for one `TASK_QUEUE_PREFIX` once.
Replicas are separate processes, so this limits only a process that embeds two apps sharing a
prefix.

Continuing as new on every server start instead would not do this alone: the new release must
replay the current run before it can continue it, and during a rolling deploy either release can
take any task.

Still to verify: what a client sees when its Update arrives as the run continues as new. The run
waits for its handlers before continuing, but an Update that arrives after that point may come
back as an error the store must retry against the new run.

### The HTTP API

The console and the CLI call the same endpoints, with the same JSON:

| Route | Body / effect |
|---|---|
| `GET /api/agents` | Every entry, without its schema, each with `retired` and worker readiness. |
| `GET /api/agents/{key}/registration` | The entry as it was registered: an `AgentRegistration`, with its queue unprefixed and its schema document, which registers it again when sent back. |
| `POST /api/agents` | `{"agents": [AgentRegistration, …]}`: register them. |
| `POST /api/agents/{key}/retire` | Retire the entry. |
| `DELETE /api/agents/{key}` | Delete the entry. |

```python
class AgentRegistration(BaseModel):
    key: str
    workflow_type: str
    task_queue: str          # as written in agents.toml; the server puts it under its prefix
    label: str
    description: str
    schema: dict[str, Any] | None  # the agent's schema document itself, not a path
```

The server checks a registration as `load_agent_registry` checks a file today: non-empty fields,
and a schema document whose `agent` is the entry's `workflow_type`. It extracts the init data and
applies the change to the store. A rule's rejection is a `409` with its message; a malformed
body is a `422`; a write still stale after its retries is a `503` the caller can retry.

`schema` is the document, never a path or an upload: the server never receives or stores a file.
The client that has the files resolves `agents.toml`'s relative `schema` paths and inlines what
they point at.

The API stays open, as the rest of it is. Authentication is a separate piece of work.

### Retired and deleted agent types

|  | In the picker | Startable | Sessions listed | Deep link to a session |
|---|---|---|---|---|
| Active | yes | yes | yes | yes |
| Retired | no | no (`409`) | yes, marked retired | yes |
| Deleted | no | no (`404`) | no | `404` |

A retired agent's sessions stay in the list and can be opened, read and messaged, because
retiring leaves the entry and its `(WorkflowType, TaskQueue)` in the visibility query.
Registering the key again brings it back.

Deleting takes the entry out of the query, so its sessions drop out of the list, as they do today
when an agent is taken out of `agents.toml`. Neither retiring nor deleting stops a running
session: it keeps running until it closes or idles out, and is still reachable by id through the
routes that never consulted the registry.

### The CLI

```
temporal-agent-kit agents add path/agents.toml [--server URL]
temporal-agent-kit agents list [--server URL]
temporal-agent-kit agents retire KEY [--server URL]
temporal-agent-kit agents delete KEY [--server URL]
```

`add` reads the file with the existing `AgentRegistryFile` model, reads each entry's `schema`
relative to the file, and sends all of the file's entries in one `POST /api/agents`, so a file
registers whole or not at all. `--server` defaults to `http://localhost:8000`, as `doctor`'s
`--api-url` does.

`serve a/agents.toml b/agents.toml` keeps working: in the app's lifespan, once the store is
entered, the server registers the files' entries through the same function `POST /api/agents`
calls, before it binds its port. For the registry workflow store that is after its worker starts,
since an Update completes only once a worker has run it. An invalid file or a rejected
registration stops `serve` with the message, as an invalid file does today. The paths become
optional, so `serve` alone starts a server over whatever the store already holds.

Since startup registration replaces entries, `serve` over a file whose agent was retired in the
console makes it active again. That follows from the files being an instruction to register, and
is the price of not having two sources.

### The console

**Adding agents: a form.** Adding agents is the Agent Bay's own action: a labelled "Add agents" button leads its header, and is the main action of an empty bay; the New session dialog has the same button. It opens a form with one row per agent: `key`,
`workflow_type`, `task_queue`, `label`, `description`, and an optional schema, attached as a
file or pasted as JSON. Rows can be added and removed, and they are sent together in one
`POST /api/agents`. No file is needed: an agent registered without a schema starts with no
init-data form and no check of its start data, as an `agents.toml` entry without `schema` does
today.

The browser checks the rows against the same rules as `AgentRegistryEntry` before it sends
anything: non-empty fields, unique keys and workflow types, and a schema that parses as JSON and
whose `agent` matches its row's `workflow_type`. Each error is shown on its row, and nothing is
sent until there are none. A `409` from the server is shown the same way. The server checks
everything again; the browser's checks reject a bad entry before it is sent.

**Importing a file fills the form.** The form takes an `agents.toml` and the schema files it
names, parses the TOML in the browser, and fills one row per `[[agents]]` entry, each with its
schema resolved. The rows can be edited before they are sent, so a file is a quicker way to fill
the form, not a separate path to the server. Keys the registry model does not name are ignored,
as on the server, so the TypeScript SDK's generated files import.

A file input gives a file's name but not its path, so the import accepts either a directory
(`webkitdirectory`, which gives each file's path inside it, so `schema = "schemas/X.json"`
resolves exactly) or several files (each `schema` matched by file name, with an error when two
selected files share one). A zip the console downloaded (below) imports the same way.

**Downloading agents: a zip.** "Download agents.toml" saves a zip of an `agents.toml` and the
schema files it names:

```
agents/
  agents.toml                  # one [[agents]] entry per active agent
  <workflow_type>.schema.json  # one per agent registered with a schema
```

Each entry's `schema` is `"<workflow_type>.schema.json"` and its `task_queue` is the unprefixed
queue it was registered with; both come from `GET /api/agents/{key}/registration`. Unzipped,
the directory is what `serve`, `agents add` and the import take, so agents set up in the console
can be committed and registered again elsewhere.

Only active agents are written: `agents.toml` has no `retired` field, and registering a file
makes its agents active, so a retired agent in the download would come back when the file is
used.

The console gains two dependencies with no dependencies of their own: `smol-toml`
(BSD-3-Clause), to parse and write TOML (browsers have no built-in parser), and `fflate` (MIT), to
write and read the zip.

**Managing an agent.** The agent dialog's details offer Edit (the add form, filled from the
agent's registration, which registers it again), Retire and Delete for an active agent, and Restore
(registering it again as it was) and Delete for a retired one. Delete asks a second time, saying
its sessions will leave the list. The dialog lists retired agents only under its Retired filter,
the home view's agent list leaves them out, and the session list marks a retired agent's sessions
"(retired)" and keeps them openable.

## What changes

- `create_agent_kit_app` takes a `registry_store` factory instead of a registry. Its lifespan
  enters the store; the three registry readers read it per request.
- A new registry module holds `RegisteredAgent`, `RegistrySnapshot`, the store interface, and the
  register/retire/delete functions. The registry workflow store and the in-memory store sit
  beside it.
- `web/registry.py` keeps the `agents.toml` models and file loading, which `serve`, `doctor` and
  `agents add` use, and gains `AgentRegistration` and the conversion from a file entry to one.
  `TASK_QUEUE_PREFIX` moves from load time to read time.
- `serve` gains `--registry-store-endpoint`, and its registry paths become optional.
- `contract/registry-store.nexusrpc.yaml` and `contract/registry-store.schema.json` publish the
  registry store's Nexus service.
- `AgentRegistryFile`'s schema (`temporal-agent-kit schema --registry`) is unchanged.
- The console gains the add form and its import, the zip download, the retire/delete controls,
  and a `retired` flag on agents and sessions.
- Tests that build an app from an in-memory `AgentRegistry` use the in-memory store. The
  registry workflow store has its own tests against the time-skipping test server, including the
  replay of a recorded history.

## Costs and limits

- **Every registry read returns the whole document**, schemas included. Listing agents or
  sessions and starting a session each read it once. For tens of agents with schemas of tens of
  kilobytes that is a few hundred kilobytes per request. If that is measured to matter, schemas
  move out of the document into content-addressed blobs it refers to by digest, which adds a
  `put_schemas` and a `get_schema` to the store interface. A per-replica cache would also save the
  reads but is server memory, so it is left out until it is measured.
- **The session query grows with agent types**, retired ones included: one `OR` clause each.
  Fine for tens of agent types; how far it stretches depends on the visibility store's query
  limits, which this design has not measured.
- **The registry workflow store keeps schema documents in workflow history.** Every write
  carries the whole document into history, and continue-as-new carries it into the next run. A
  document large enough to pass Temporal's payload size limit is offloaded by the `AgentKitPlugin`
  large-payload storage the server's client and its registry worker already use.
- **The registry workflow store needs its worker.** It runs in the server, so this only matters to
  a process that embeds `create_agent_kit_app` and expects to read agents without its lifespan.
- **Open API.** Anyone who can reach the server can register a type that starts any workflow
  type on any task queue under its prefix, with the server's Temporal credentials. That is the
  reason authentication comes next, not a reason to hold this back.
