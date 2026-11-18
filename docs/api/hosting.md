# Self-hosting the web app

Most projects want the packaged CLI above — `temporal-agent-kit serve` runs exactly this app.
Reach for the factories here when you need the Agent Kit API and UI *inside* your own FastAPI
service: your own routes alongside it, auth middleware, a registry built in code rather than
read from a TOML file, or a different ASGI deployment.

`create_agent_kit_app` returns the FastAPI app, serving both `/api/*` and the packaged Svelte
UI. `create_session_manager_worker` builds the same agent-agnostic worker that
`temporal-agent-kit session-manager` runs for you:

```python
from temporalio.client import Client
from temporalio.envconfig import ClientConfig

from temporal_agent_kit.plugin import AgentKitPlugin
from temporal_agent_kit.web import (
    create_agent_kit_app,
    create_session_manager_worker,
)


async def run_session_manager() -> None:
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[AgentKitPlugin()])
    worker = create_session_manager_worker(client)
    await worker.run()


app = create_agent_kit_app(registry_path="agents.toml")
```

Then serve the app with Uvicorn:

```bash
uvicorn my_app.web:app --host 0.0.0.0 --port 8000
```

`create_session_manager_worker` only registers the packaged session-manager workflow; run your
own agent workflows on their own workers and task queues. It takes an already-connected client,
so the Agent Kit plugin goes on that client — see
[Running a worker](../../python/README.md#running-a-worker--one-plugin).
