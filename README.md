# Temporal Agent Harness

**Build durable, composable AI agents with a rich tool-approval policy engine that can seamlessly elevate to a human with built-in human-in-the-loop.**

> ⚠️ **Experimental.** An early, fast-moving project from Temporal Technologies. APIs will change.

The Temporal Agent Harness gives your agents capabilities that are painful and error-prone to
build yourself:

- agents **survive crashes and resume mid-turn**, exactly where they left off (no wasted tokens!);
- a **rich tool-approval policy engine** decides exactly when a tool call needs human
  sign-off — handing control to a human only then, and resuming the moment it's granted —
- agents **compose programmatically**, through real typed contracts (not limited to just text in,
  text out);
- agents are **fully observable** — a standardized, full-lifecycle event stream lets you watch them
  live or replay exactly what they did;

all while you write the actual turn logic with the **AI SDKs you already know**.

Every agent is a durable Temporal workflow at its core, giving agents access to the full breadth
of Temporal primitives. Tools as activities or workflow functions, every turn a streamed,
replayable history. The harness packages those primitives into a toolkit built for first-class
agent development, so you get the power without hand-rolling the orchestration.

## What you get

### 🛡️ Durable execution — the foundation
Every agent is a Temporal workflow, so durability isn't a feature you add, it's the ground you
build on. A worker can crash, redeploy, or restart mid-turn and the agent resumes precisely where
it was — no lost state, no double-run tool calls. Model and tool calls retry by policy; an agent
can wait minutes or days for an external event without holding a process open; and every turn,
tool call, and decision is recorded and replayable.

### 📊 Standardized agents, fully observable
The interface to agents built on this harness is **carefully standardized**. Every agent takes the
same configuration contract and emits the same structured **event stream**: a protocol (under
active development) that captures an agent's entire lifecycle — every turn, model interaction, tool
call (start / end / error), reply token, citation, approval decision, subagent hand-off, and
token-usage tally.

That one standardized stream is the foundation of **observability and analytics** for every agent
you build. **Watch an agent live** as it works, or **replay exactly what happened** afterward —
what it decided, which tools it ran, what it cost, and where a human stepped in. You instrument
once; every agent on the harness gets it.

### 🙋 Human-in-the-loop, solved
Tool approvals are built in and **safe-by-default**: any tool call can require human sign-off, and
a gated call **pauses inside the workflow and resumes durably** whenever a decision arrives (no
matter how long that takes) — there's no approval queue, state machine, or callback plumbing for
you to build. The policy engine is sophisticated out of the box: layered rules, inherently-safe
auto-approval, per-tool allow-lists, "approve and stop asking," per-session overrides, runtime
policy updates, and custom predicates.

### 🔌 Bring your own AI SDK
Write turn logic with the SDK you already know. The harness's integrations turn each SDK call into
a durable Temporal activity — so retries and credentials never leak into your workflow code.
Support is growing across the Python AI SDKs and agent frameworks Temporal integrates with:

| AI SDK | Status | Notes |
| --- | --- | --- |
| [Google Gemini](temporal_agent_harness/ai_sdks/google_genai_plugin) | ✅ Available now | Ships in this repo and is **experimental** — not an officially supported integration in the Temporal Python SDK. |
| [OpenAI Agents SDK](https://github.com/temporalio/sdk-python/blob/main/temporalio/contrib/openai_agents/README.md) | 🟡 Planned | Vendor [implementation](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) to add harness support. |
| [Pydantic AI](https://ai.pydantic.dev/durable_execution/temporal/) | 🟡 Planned | Vendor [implementation](https://ai.pydantic.dev/durable_execution/temporal/) to add harness support. |
| [Google ADK](https://adk.dev/integrations/temporal/) | 🟡 Planned | Vendor [implementation](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/google_adk_agents) to add harness support. |
| [Strands Agents](https://docs.temporal.io/develop/python/integrations/strands-agents) | 🟡 Planned | Vendor [implementation](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/strands) to add harness support. |
| [LangGraph](https://docs.temporal.io/develop/python/integrations/langgraph) | 🟡 Planned | Vendor [implementation](https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/langgraph) to add harness support. |

### 📡 Durable and inline tools
Tools come in two flavors — durable, activity-backed tools (`@agent.activity_tool_defn`) that run
as retried, observable Temporal activities, and inline workflow tools (`@agent.tool_defn`). Each
publishes its own start/end lifecycle events onto the agent's standardized event stream.

### 🧩 Agents that are more than chatbots
Most frameworks treat an agent as a single text-in / text-out function. Here, an agent exposes a
**strongly-typed interface**: declare named operations with `@agent.accepts` over pydantic models
(plain text is just one shape). The agent **advertises its own callable surface** — operation
names and input/output schemas — so it's self-describing and ready to be driven programmatically,
by your code or by another agent.

### 🔗 Composable by construction
Because agents have typed, self-describing interfaces, any harness agent can become a
**strongly-typed toolset** another agent drives — start it, call its operations, stop it.
Multi-agent systems compose through real contracts, not strings pasted between prompts.


## A taste

```python
from datetime import timedelta

from pydantic import BaseModel
from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import AgentConfig, ToolApprovalPolicy


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


@agent.defn
class TravelAgent:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        # Tool approvals are safe-by-default; here, auto-approve only tools that
        # statically declare themselves inherently safe.
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)

    # A typed, self-describing operation. The agent advertises this signature, so callers —
    # your code or another agent — can drive it programmatically. Your turn logic goes here:
    # call your AI SDK, run tools through the runner, and return the typed reply.
    # (See examples/monty for a complete, model-in-the-loop agent on the Gemini integration.)
    @agent.accepts
    async def plan_trip(self, request: PlanTrip) -> Itinerary:
        ...
```

## Repository layout

```
temporal_agent_harness/
├── harness/      # the core harness: agent contract, turn runner, tools,
│                 #   the agent/subagent protocol, human-in-the-loop approvals
├── ai_sdks/      # AI SDK integrations (Gemini today) — durable activity wrappers
├── web/          # packaged session-manager workflow + FastAPI app factory
└── utils/        # general Temporal utilities (e.g. large-payload offload)

examples/
├── monty/            # a travel-booking agent example (start here)
└── session_manager/  # example registry/wrappers for the packaged web layer

ui/               # shared Svelte frontend for examples

tests/            # mirrors the package layout
```

## Requirements

- Python **3.11+**
- [uv](https://docs.astral.sh/uv/) for dependency management
- [just](https://just.systems/) for the example recipes
- [pnpm](https://pnpm.io/) for building or developing the Svelte UI
- A Temporal service. The examples can start a local dev server with `just temporal`
  if you have the `temporal` CLI installed.

## Root Justfile

The repo root has a `justfile` for the common development workflow. Run
`just --list` from the repo root to see the available recipes.

Build and package recipes run directly from the root:

```bash
just app-install   # install Svelte dependencies
just app-build     # build ui/ into temporal_agent_harness/ui/dist
just app-check     # Svelte checks
just package       # UI build + UI checks + pytest + uv build
```

Local stack recipes delegate into `examples/monty`:

```bash
just temporal          # local Temporal dev server
just session-manager   # packaged session-manager worker
just server            # built Svelte UI + FastAPI API on http://localhost:8000
just monty-worker      # Monty agent worker
just ui-dev            # Vite hot reload on http://127.0.0.1:5173
```

The delegation matters because `examples/monty/justfile` loads
`examples/monty/.env.local`. Keep example-specific settings there; root commands
will still use them.

## Run The Example

The [`examples/monty`](examples/monty) example is the best end-to-end path: it
includes a conversational travel agent and a subagent-driven variant. From
`examples/monty`, create local environment settings first:

```bash
cp .env.example .env.local
```

Set `GEMINI_API_KEY` in `.env.local` for the conversational agents. The example
defaults to the committed `temporal.local.toml` profile, which points at a local
Temporal dev server.

Install the Svelte UI dependencies once from the repo root:

```bash
just app-install
```

Run each command in its own terminal from the repo root:

```bash
just temporal          # local Temporal dev server; skip if you bring your own
just session-manager   # worker hosting the packaged SessionManagerWorkflow
just server            # builds and serves the Svelte UI + /api on http://localhost:8000
just monty-worker      # Monty agent worker
```

These root recipes delegate into `examples/monty`, so `.env.local` is still read
from that example directory. You can also run the same recipes directly from
`examples/monty`; there the agent worker recipe is named `just worker`.

Open <http://localhost:8000> and select a Monty agent. `just server` runs
`app-build` first, so port 8000 serves the current built Svelte UI from
`temporal_agent_harness/ui/dist`, not the legacy static HTML files.

Useful UI recipes are available from the root and from the imported example
justfiles:

```bash
just app-install   # install Svelte dependencies
just app-check     # svelte-check + local Svelte 5 syntax guard
just app-build     # build ui/ into temporal_agent_harness/ui/dist
just ui-dev        # Vite hot reload on http://127.0.0.1:5173, proxying /api to :8000
```

Use `just ui-dev` only for frontend iteration. Keep `just server` running too,
because the Vite dev server proxies API calls to the FastAPI server on port 8000.

## Using The Package

Install the web/UI extra when you want the reusable FastAPI server and packaged
browser UI:

```bash
pip install "temporal-agent-harness[ui]"
```

Agent authors use the harness runtime from `temporal_agent_harness.harness`.
Applications that want the built-in session manager and UI use
`temporal_agent_harness.web`:

```python
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig

from temporal_agent_harness.utils.large_payload import with_large_payload_offload
from temporal_agent_harness.web import (
    create_agent_harness_app,
    create_session_manager_worker,
)


async def run_session_manager() -> None:
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        data_converter=await with_large_payload_offload(pydantic_data_converter),
    )
    worker = create_session_manager_worker(client)
    await worker.run()


app = create_agent_harness_app(registry_path="agents.toml")
```

Then serve the app with Uvicorn:

```bash
uvicorn my_app.web:app --host 0.0.0.0 --port 8000
```

The registry lists the launchable agents the UI can create:

```toml
[[agents]]
key = "my-agent"
workflow_type = "MyAgent"
task_queue = "my-agent-task-queue"
label = "My Agent"
description = "A short description shown in the UI."
```

The app factory serves both `/api/*` and the packaged Svelte UI. The helper
`create_session_manager_worker` only registers the packaged session-manager
workflow; run your own agent workflows on their own workers and task queues.

## UI Development

The source Svelte app lives in [`ui/`](ui). The package ships the compiled
output in [`temporal_agent_harness/ui/dist`](temporal_agent_harness/ui/dist), so
any UI change needs a rebuild before packaging or before `just server` serves it:

```bash
just app-install   # one-time install of Svelte dependencies
just app-build     # build ui/ into temporal_agent_harness/ui/dist
just app-check     # svelte-check + local Svelte 5 syntax guard
```

For hot reload, keep the FastAPI API server running on port 8000 and start Vite
in another terminal:

```bash
# terminal 1
just server

# terminal 2
just ui-dev
```

The production build uses relative asset and API URLs, so the UI can be served
from `/` or under a path prefix as long as the UI and API are mounted together.

## Build And Package

Use the package recipe from the repo root:

```bash
just app-install   # one-time setup if ui/node_modules is absent
just package
```

The resulting artifacts are written to `dist/`:

```text
dist/temporal_agent_harness-0.1.0.tar.gz
dist/temporal_agent_harness-0.1.0-py3-none-any.whl
```

The wheel and sdist include:

- the core harness package
- `temporal_agent_harness.web` with the FastAPI app factory and session-manager worker helper
- `temporal_agent_harness.ui/dist` with the built Svelte UI assets
- the `ui` extra, which pulls in `fastapi[standard]`

Before publishing or handing off artifacts, run:

```bash
just package
```

`just package` runs the Svelte production build, Svelte checks, the local
Svelte 5 syntax guard, the Python test suite, and `uv build`. The primary
recipe lives in the repo-root `justfile`; the same recipe is also available
from the example justfiles for convenience.

## Status & docs

This is experimental and under active development; expect breaking changes. Deeper design
documentation — the agent protocol, the streaming model, human-in-the-loop approvals, and
agents-as-subagents — is being written and will land under [`docs/`](docs).
