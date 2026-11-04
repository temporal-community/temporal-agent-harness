# Temporal Agent Harness for Python

Build durable agents using Python, Temporal, and your preferred AI SDK.

[Project overview](https://github.com/temporal-community/temporal-agent-harness/blob/main/README.md) · [TypeScript guide](https://github.com/temporal-community/temporal-agent-harness/blob/main/typescript/packages/core/README.md) · [Harness API](https://github.com/temporal-community/temporal-agent-harness/blob/main/docs/api/README.md)

## Installation

Install from PyPI in your own project, or run the repository examples.

### Try the example agents

Clone at a release tag. You need [just](https://just.systems/),
[Bazelisk](https://github.com/bazelbuild/bazelisk) installed as `bazel`, and the
[`temporal` CLI](https://docs.temporal.io/cli) (see [Requirements](https://github.com/temporal-community/temporal-agent-harness/blob/main/docs/guides/examples.md#requirements)).

```bash
git clone --branch 0.8.0 https://github.com/temporal-community/temporal-agent-harness.git
cd temporal-agent-harness
cp .env.example .env.local   # then set GEMINI_API_KEY (and/or OPENAI_API_KEY)
```

Then run two commands in separate terminals to get going with all example agents:

```bash
just temporal       # 1. local Temporal dev server (needs the `temporal` CLI)
just dev            # 2. session-manager, harness server and all example agent workers
```

Open <http://localhost:8000> and start a session. There's no install step — the recipes build with
Bazel, which fetches dependencies on first run. Every example follows the same four recipes; see
[Run the examples](https://github.com/temporal-community/temporal-agent-harness/blob/main/docs/guides/examples.md#run-the-examples) for the rest of them, including running all eight behind
one UI.

Git will note that you're in "detached HEAD" — that's expected, it just means you're sitting on
the tag rather than on a branch. Later, move to a newer release with
`git fetch --tags && git checkout <version>`, or see what changed between two of them with
`git diff 0.7.0 0.8.0`.

### Build with it — install from PyPI

Use this path to build your own agents in your own project. You don’t need to clone this repository.

The harness is published to
[PyPI](https://pypi.org/project/temporal-agent-harness/). In a
[`uv`](https://docs.astral.sh/uv/)-managed project:

```bash
uv add 'temporal-agent-harness[ui]==0.8.0'
```

Or declare it in `pyproject.toml` — an ordinary dependency, no `[tool.uv.sources]` needed:

```toml
[project]
dependencies = [
    # Everything past the base install is an opt-in extra — combine as many as you need
    # in the one spec, e.g. [ui,code-mode,genai]:
    #
    #   ui              the browser UI/API server dependencies
    #   code-mode       the sandbox a worker runs Code Mode scripts in
    #   genai           the Google Gemini integration
    #   openai-agents   the OpenAI Agents SDK integration
    #   pydantic-ai     the Pydantic AI integration
    #   typesafe        typed decisions and the Jev auto mode evaluator, via the canonical
    #                   temporalio-typesafe plugin
    #   s3              S3-backed offload for large payloads
    #
    # What each one pulls in, and when you actually need it, is in the Extras table below.
    "temporal-agent-harness[ui]==0.8.0",
]
```

Then run `uv sync`. Pin the exact version: this project is pre-1.0 and APIs change between
releases — see [Versioning and stability](#versioning-and-stability).

#### Run the web UI — no checkout, no Node

The package installs a `temporal-agent-harness` command; the `ui` extra adds its server runtime —
`uv run` it, the way you would any other project tool. It serves the browser UI built into the
wheel: nothing to clone, nothing to build. One command per terminal:

```bash
# 1. A local Temporal dev server (needs the `temporal` CLI). Its own Web UI: http://localhost:8233
temporal server start-dev

# 2. The packaged session-manager worker. It hosts SessionManagerWorkflow, which starts your
#    agents as child workflows — without it the server comes up but has nothing to answer
#    /api/agents or session creation, since both are workflow queries.
uv run temporal-agent-harness session-manager

# 3. The prebuilt Svelte UI + JSON API on http://localhost:8000
uv run temporal-agent-harness serve ./agents.toml

# 4. Your own worker, hosting your agent workflows on their own task queue
uv run python -m my_app.worker
```

Open <http://localhost:8000> and create a session. `serve` accepts several registries and merges
them (`serve one/agents.toml two/agents.toml`), so a single UI can list agents from more than one
project.

That registry is what lists the launchable agents:

```toml
[[agents]]
key = "my-agent"
workflow_type = "MyAgent"
task_queue = "my-agent-task-queue"
label = "My Agent"
description = "A short description shown in the UI."
schema = "MyAgent.schema.json"   # optional; see below
```

`schema` is the agent's schema document, relative to the registry, which the
`schema` subcommand writes from its class:

```bash
uv run temporal-agent-harness schema my_app.workflow:MyAgentWorkflow -o MyAgent.schema.json
```

The server reads what the agent takes to start from it and never imports the agent's code. If its
`@agent.init` takes a `data` model after its config, required (`data: MyData`) or optional
(`data: MyData | None = None`), `GET /api/agents` publishes it and the console asks for it in a
form before starting a session. `POST /api/sessions` answers `422` when `data` is missing for an
agent that requires it or sent to one that takes none; the agent validates the data itself. Without
`schema`, any `data` a caller sends goes through for the agent to validate. Regenerate the schema
when the agent's init data changes. (This repo's examples have Bazel generate each `agents.toml`
and its schemas; see `bazel/agents.bzl`.)

Both subcommands resolve their Temporal connection through temporalio's standard client config,
so they land on the same namespace. With nothing configured they use `localhost:7233` — the
`start-dev` default. Note that a `temporal.toml` in the **current directory is not picked up
automatically**; point at a project-local file (or a Temporal Cloud profile) explicitly:

```bash
TEMPORAL_CONFIG_FILE=./temporal.toml TEMPORAL_PROFILE=cloud \
    uv run temporal-agent-harness serve ./agents.toml
```

`uv run temporal-agent-harness --help` documents the full precedence order.

Need the API and UI mounted inside your own FastAPI service instead? See
[Self-hosting the web app](https://github.com/temporal-community/temporal-agent-harness/blob/main/docs/api/hosting.md#self-hosting-the-web-app).

#### Extras

Agent authors import the runtime from `temporal_agent_harness.harness`; the base install carries
only `temporalio` and `pydantic`, so nothing drags in an AI SDK you don't use. Everything else is
opt-in:

| Extra | Add it when you… |
| --- | --- |
| `ui` | want the browser UI and HTTP API server (pulls in `fastapi[standard]`, including Uvicorn). The built Svelte assets are always in the wheel; only the server runtime is gated here, so agent-worker installs stay small. |
| `code-mode` | run a worker that hosts **Code Mode** agents; pulls in [`pydantic-monty`](https://pypi.org/project/pydantic-monty/), the sandbox the scripts run in. Importing `agent` needs nothing extra; building a `code_mode_tool` does. |
| `genai` | use the **Google Gemini** integration (`ai_sdks.google_genai_plugin`). |
| `openai-agents` | use the **OpenAI Agents SDK** integration (`ai_sdks.openai_agents`). |
| `pydantic-ai` | use the **Pydantic AI** integration (`ai_sdks.pydantic_ai_harness`). |
| `typesafe` | make typed [TypeSafe](https://docs.typesafe.ai) System One calls (`ai_sdks.typesafe_harness`), or use **`ai_sdks.jev.jev_evaluator`**, the Jev-backed auto mode evaluator; pulls in [`temporalio-typesafe`](https://pypi.org/project/temporalio-typesafe/). Register its `TypeSafePlugin` on the worker, before `AgentHarnessPlugin`. |
| `s3` | offload large payloads to S3. The default local-filesystem driver needs nothing extra. |

Combine them in one spec, e.g. `uv add 'temporal-agent-harness[ui,code-mode,genai]==0.8.0'`.


## Versioning and stability

**Pin to a release.** Every release is listed on the
[releases page](https://github.com/temporal-community/temporal-agent-harness/releases) and
published to [PyPI](https://pypi.org/project/temporal-agent-harness/). A release tag marks a
commit that is known-good at the moment it was cut: the tests passed, and the published wheel and
sdist were built from that commit, browser UI included.

Two artifacts come out of a release, and they pin differently:

- **The library** — pin an exact version from PyPI (`temporal-agent-harness==0.8.0`).
- **The examples** — check out the matching git tag. They are *not* shipped in the package, so a
  PyPI install gives you the library, the packaged UI, and the CLI, but no `examples/` tree.

**This project is very early and experimental - `main` may break at any time.**

Releases are cut when the maintainers judge the state stable ([how](https://github.com/temporal-community/temporal-agent-harness/blob/main/RELEASE.md)). This project is
**experimental** and pre-1.0: APIs will change between releases, without warning. Pinning is what
keeps that churn from reaching you unannounced — so pin, and upgrade deliberately when you want
to try out new harness capabilities.


## A taste

```python
from datetime import timedelta

from pydantic import BaseModel
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import AgentConfig, MidTurn, ToolApprovalPolicy


# A durable, activity-backed tool: runs as a retried, observable Temporal activity and
# publishes its own tool_start/tool_end events on the turn stream.
@agent.activity_tool_defn(
    activity_config=ActivityConfig(start_to_close_timeout=timedelta(seconds=30)),
)
async def search_flights(origin: str, destination: str, date: str) -> str:
    ...


# Strongly-typed messages — an agent operation is more than a string in and a string out.
class PlanTrip(BaseModel):
    destination: str
    nights: int


class Itinerary(BaseModel):
    summary: str
    total_usd: float


# The agent's own init data, passed after the AgentConfig when a session starts.
class TravelerProfile(BaseModel):
    """Who the agent is planning for."""

    name: str
    home_airport: str


# @agent.defn makes the class a Temporal workflow and provides its run method, which drives
# the turn loop. Add an optional `@agent.setup` / `@agent.teardown` method for one-time async
# work before the first message or after the agent closes.
@agent.defn
class TravelAgent:
    @agent.init
    def __init__(self, config: AgentConfig, profile: TravelerProfile | None = None) -> None:
        # Tool approvals are safe-by-default; here, auto-approve only tools that
        # statically declare themselves inherently safe.
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        # Optional, so a caller may start it without a profile. Declare it without the
        # `| None = None` to make it required.
        self._profile = profile

    # A typed, self-describing operation. The agent advertises this signature, so callers —
    # your code or another agent — can drive it programmatically. Your turn logic goes here:
    # call your AI SDK, run tools through the runner, and return the typed reply.
    # (See examples/monty for a complete, model-in-the-loop agent on the Gemini integration.)
    @agent.accepts
    async def plan_trip(self, request: PlanTrip) -> Itinerary:
        ...
```


## Running a worker — one plugin

An agent is a Temporal workflow, so it runs on a Temporal worker. `AgentHarnessPlugin` is the
single registration that wires that worker (and its client) for the harness — you never
assemble the harness's activity list or its data converter by hand:

```python
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.plugin import AgentHarnessPlugin

client = await Client.connect(
    **ClientConfig.load_client_connect_config(),
    # Your AI SDK's plugin first, the harness plugin last.
    plugins=[OpenAIAgentsPlugin(model_params=...), AgentHarnessPlugin(tools=MY_TOOLS)],
)

worker = Worker(client, task_queue="my-agent", workflows=[TravelAgent])
await worker.run()
```

The worker declares only its workflows. Adding the plugin brings:

- the harness's **data converter** (Pydantic + large-payload offload) — add the plugin to every
  client, worker, and server in the deployment so they all agree on it;
- the durable **activity body** of each `@agent.activity_tool_defn` tool in `tools=` (pass your
  whole toolset — tools with no worker-side body are skipped);
- the **subagent** and **Code Mode** activities.

Order it last, after any AI SDK's plugin, so that SDK's payload converter wins. Registering it
on the client is enough — Temporal applies a client's plugins to workers built from it.


## Coding-agent setup and starter

These commands are new for the next Python release; they are not in the published 0.8.0 package.

After installing the SDK, create an OpenAI starter and install this release's coding-agent skill:

```bash
uv run temporal-agent-harness init ./my-agent
cd my-agent
uv sync
uv run temporal-agent-harness skills install
```

Follow the starter's `README.md` to configure credentials, generate its schema, and start the
four services. `skills install` writes `.agents/skills/temporal-harness-python` in your project;
run it again after upgrading. It refuses to overwrite local edits. `skills path` prints the
bundled skill location. These development commands ship with the base package. Serving the API/UI and exporting
OpenAPI require the `ui` extra.

```bash
uv run temporal-agent-harness doctor --registry agents.toml --api-url http://localhost:8000
```

The doctor is read-only: it checks the registry and schemas, Temporal connectivity, task queue
pollers, and the shared API. `--json` prints a machine-readable report; a failed check exits 1.

## Further reading

- [Harness API and session lifecycle](https://github.com/temporal-community/temporal-agent-harness/blob/main/docs/api/README.md)
- [Messages and handler behavior](https://github.com/temporal-community/temporal-agent-harness/blob/main/docs/guides/messages.md)
- [Approvals and auto mode](https://github.com/temporal-community/temporal-agent-harness/blob/main/docs/guides/approvals.md)
- [Code Mode, files, and subagents](https://github.com/temporal-community/temporal-agent-harness/blob/main/docs/guides/code-mode.md)
- [Build a React or Svelte UI](https://github.com/temporal-community/temporal-agent-harness/blob/main/docs/guides/client-ui.md)
- [Run the repository examples](https://github.com/temporal-community/temporal-agent-harness/blob/main/docs/guides/examples.md)
- [Embed the web app in FastAPI](https://github.com/temporal-community/temporal-agent-harness/blob/main/docs/api/hosting.md)
