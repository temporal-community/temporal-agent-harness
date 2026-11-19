# Self-hosting the web app

Most projects want the packaged CLI above — `temporal-agent-kit serve` runs exactly this app.
Reach for the factories here when you need the Agent Kit API and UI *inside* your own FastAPI
service: your own routes alongside it, auth middleware, a registry built in code rather than
read from a TOML file, or a different ASGI deployment.

`create_agent_kit_app` returns the FastAPI app, serving both `/api/*` and the packaged Svelte
UI. It starts each session as the agent's workflow and lists sessions from Temporal visibility,
so it needs no worker of its own:

```python
from temporal_agent_kit.web import create_agent_kit_app

app = create_agent_kit_app(registry_path="agents.toml")
```

Then serve the app with Uvicorn:

```bash
uvicorn my_app.web:app --host 0.0.0.0 --port 8000
```

Run your agent workflows on their own workers, on the task queues the registry names, with the
Agent Kit plugin — see [Running a worker](../../python/README.md#running-a-worker--one-plugin).
