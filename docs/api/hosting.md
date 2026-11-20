# Self-hosting the web app

Most projects want the packaged CLI above — `temporal-agent-kit serve` runs exactly this app.
Reach for the factories here when you need the Agent Kit API and UI *inside* your own FastAPI
service: your own routes alongside it, auth middleware, a registry kept in your own database, or a
different ASGI deployment.

`create_agent_kit_app` returns the FastAPI app, serving both `/api/*` and the packaged Svelte
UI. It starts each session as the agent's workflow and lists sessions from Temporal visibility.
Its agent registry lives in a registry store: by default a Temporal workflow, served by a worker
the app runs while it is up. `registrations` are registered when the app starts, as
`temporal-agent-kit serve agents.toml` registers its files:

```python
from pathlib import Path

from temporal_agent_kit.web import create_agent_kit_app, load_registrations

app = create_agent_kit_app(registrations=load_registrations(Path("agents.toml")))
```

Agents can also be registered, retired and deleted while the app runs, through
`POST /api/agents`, `POST /api/agents/{key}/retire` and `DELETE /api/agents/{key}`, which the
console and `temporal-agent-kit agents` use.

To keep the registry in a database instead, pass `registry_store`: a factory that takes the app's
Temporal client and returns an async context manager yielding an
`temporal_agent_kit.web.registry_store.AgentRegistryStore`. A store implements two methods over
one versioned document, `read()` and a `write(snapshot, expected)` that stores the snapshot only if
the stored version is still `expected`; the registry's rules run in the app. `serve` takes the
same factory as `--registry-store module.path:factory`.

Then serve the app with Uvicorn:

```bash
uvicorn my_app.web:app --host 0.0.0.0 --port 8000
```

Run your agent workflows on their own workers, on the task queues the registry names, with the
Agent Kit plugin — see [Running a worker](../../python/README.md#running-a-worker--one-plugin).
