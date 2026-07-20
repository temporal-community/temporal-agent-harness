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
- **Code Mode** — one tool that runs a Python script over your toolset — so a single
  turn orchestrates many tool calls with real control flow and concurrency;
- **callback tools** let an agent invoke a tool that runs on the *client* — reading a file on a
  user's laptop, capturing a photo on their phone — even though the agent runs on a remote worker;
- agents are **fully observable** — a standardized, full-lifecycle event stream lets you watch them
  live or replay exactly what they did;

all while you write the actual turn logic with the **AI SDKs you already know**.

Every agent is a durable Temporal workflow at its core, giving agents access to the full breadth
of Temporal primitives. Tools as activities or workflow functions, every turn a streamed,
replayable history. The harness packages those primitives into a toolkit built for first-class
agent development, so you get the power without hand-rolling the orchestration.

## Installation

Add it to your project as a **git dependency**. In a [`uv`](https://docs.astral.sh/uv/)-managed project, the quickest way is `uv add`:

```bash
# core harness — define and run agent workflows
uv add "temporal-agent-harness @ git+https://github.com/temporal-community/temporal-agent-harness.git"
```

Or declare it in `pyproject.toml` — depend on the package (with any extras you need) and point
its source at the git repository:

```toml
[project]
dependencies = [
    "temporal-agent-harness[ui]",
]

[tool.uv.sources]
temporal-agent-harness = { git = "https://github.com/temporal-community/temporal-agent-harness.git", branch = "main" }
```

Then run `uv sync`. (Pin to a specific `rev = "..."` instead of `branch = "main"` for a
reproducible build.)

**Extras:**

- **`ui`** — the reusable FastAPI server and packaged browser UI (pulls in `fastapi[standard]`,
  including Uvicorn). The built Svelte assets are always in the artifact; only the server runtime
  dependencies are gated behind this extra, so core agent-worker installs stay smaller.
- **`code-mode`** — for workers that host **Code Mode** agents; pulls in
  [`pydantic-monty`](https://pypi.org/project/pydantic-monty/), the sandbox the scripts run in.
  (The workflow-side `agent.code_mode_tool` factory itself needs nothing extra, so importing it
  never requires this dependency.)

Combine extras in the dependency spec, e.g. `"temporal-agent-harness[ui,code-mode]"`.

Agent authors use the harness runtime from `temporal_agent_harness.harness`. Applications that
want the built-in session manager and UI use `temporal_agent_harness.web`:

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
Tools come in two on-worker flavors — durable, activity-backed tools (`@agent.activity_tool_defn`)
that run as retried, observable Temporal activities, and inline workflow tools
(`@agent.tool_defn`). Each publishes its own start/end lifecycle events onto the agent's
standardized event stream. (A third flavor — **callback tools** — runs on an attached client
instead of the worker; see below.)

### 📞 Callback tools — let the client run the tool
An agent running on a Temporal worker often needs to act somewhere it can't reach — a file on the
user's laptop, a photo from their phone, a device on a private network. A **callback tool**
(`@agent.callback_tool_defn`) has no worker-side body: the agent **pauses inside the workflow**,
publishes the call, and an **attached client executes it on its own machine** and sends the result
back. You declare only the tool's typed contract (its `...` body is enforced) — the harness
supplies the single generic implementation. Because it's dispatched like any other tool, a callback
tool inherits the *same* approval policy, `tool_start`/`tool_end` events, and durable pause/resume:
the workflow simply waits (seconds or days) until a result arrives, and that result is validated
against the tool's declared output type before the turn continues. See
[`examples/callback_tools/wiki_agent`](examples/callback_tools/wiki_agent) — a cloud-shaped agent
that organizes a Markdown wiki on *your* local disk through a thin terminal client.

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

### 🧑‍💻 Code Mode — give the model code, not just a call menu
Hand a model **one tool that runs a Python script** over your existing tools, instead of a long
menu of individual calls. `agent.code_mode_tool([...tools...])` turns any set of harness tools
into a single run-a-script tool: the model writes Python that calls them as async host functions,
with real control flow — loops, conditionals, `min`/`max`, and `asyncio.gather` concurrency — so
one turn orchestrates many calls. Each host call is still dispatched through the runner as a
durable, approval-gated, observable activity, and the script is statically type-checked against
your tools' signatures **before it runs**. And since a subagent toolset is just a list of tools,
Code Mode composes over subagents for free.


## A taste

```python
from datetime import timedelta

from pydantic import BaseModel
from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import AgentWorkflowRunner, agent, slash_commands
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
            slash_commands=slash_commands.default_commands(),
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

## Code Mode

Most agents call tools one at a time — a round-trip per call. **Code Mode** hands the model a
single tool that runs a Python *script* over your tools, so one turn can search, filter, branch,
and act across many calls with ordinary control flow and `asyncio.gather` concurrency.

`agent.code_mode_tool(tools, name=...)` takes any list of harness tools and returns one inline
tool. Its generated description tells the model the sandbox contract and every host function's
signature + result shape — derived from your tools, so you never hand-write or maintain it. Hand
the returned tool to your model's tool-calling loop like any other tool.

```python
from temporal_agent_harness.harness import agent

# Any @agent.activity_tool_defn / @agent.tool_defn tools — including a subagent toolset.
run_code = agent.code_mode_tool(
    [search_flights, search_hotels, book_flight, get_trip_summary],
    name="run_travel_code",
)
# The model then writes, e.g., a script like this and calls run_travel_code with it:
#
#     import asyncio
#     async def main():
#         flights, hotels = await asyncio.gather(  # independent calls run concurrently
#             search_flights({"origin": "SFO", "destination": "JFK", "date": "2026-07-01"}),
#             search_hotels({"city": "New York", "check_in": "2026-07-01", "check_out": "2026-07-05"}),
#         )
#         cheapest = min(flights["flights"], key=lambda f: f["price_usd"])
#         return await book_flight({"flight_id": cheapest["flight_id"], "passenger_name": "Ada Lovelace"})
#     asyncio.run(main())
```

- **Durable, gated, and observable per call.** The script runs in a sandbox; each host call is
  dispatched back through the runner as its own durable activity — keeping that tool's approval
  policy and `tool_start`/`tool_end` events. Writing the script is inert; only the host calls act.
- **Type-checked before it runs.** Code Mode generates static type-check stubs from your tools'
  signatures, so a wrong argument or an unknown result key comes back as an error to fix rather
  than a bad run.
- **Composes over subagents.** `agent.subagent_toolset(...)` returns a list of tools, so drop it
  straight into `code_mode_tool([...])` — the model's script can drive subagents too.
- **Several per agent.** Give one agent multiple `code_mode_tool`s (distinct `name`s) over
  disjoint or overlapping tool sets.

A worker that hosts a Code Mode agent registers the two sandbox-stepping activities (this needs
the `code-mode` extra, which pulls in [`pydantic-monty`](https://pypi.org/project/pydantic-monty/),
the sandbox scripts run in) alongside the durable bodies of any activity-backed host tools:

```python
from temporal_agent_harness.harness.code_mode.activities import CODE_MODE_ACTIVITIES

worker = Worker(
    client,
    task_queue=...,
    workflows=[MyAgent],
    activities=[*CODE_MODE_ACTIVITIES, *(agent.tool_activity(t) for t in my_activity_tools)],
)
```

See [`examples/monty`](examples/monty) for three agents all built on Code Mode: a no-model script
runner, a conversational agent that writes its own scripts, and a subagent-driven variant.

## Slash Commands

Agents can expose human/operator slash commands through a small library of
workflow-safe command definitions. A command bundles the UI metadata returned by
the `operator_interface` query with the deterministic handler that runs inside
the workflow.

If `slash_commands` is omitted, `AgentWorkflowRunner` enables the packaged
defaults:

| Command | Effect |
| --- | --- |
| `/approvals strict\|safe\|skip` | Change the live tool-approval policy. |
| `/allow-tools tool_name` | Auto-approve one or more named tools for this session. |
| `/status` | Show the current harness status. |
| `/stop` | Stop the agent workflow. |

Configure exactly the packaged commands you want in one place:

```python
from temporal_agent_harness.harness import slash_commands

self._runner = AgentWorkflowRunner(
    config,
    stream=WorkflowStream(),
    approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
    slash_commands=slash_commands.commands("approvals", "status", "stop"),
)
```

Pass an empty list to disable packaged slash commands:

```python
slash_commands=[]
```

Custom commands use the same registry. For example, a model selector can share
one implementation across the first-class operator update path and the normal
`slash` turn path:

```python
SUPPORTED_MODELS = ("gemini-3.5-flash", "gemini-3.1-flash-lite")

self._runner = AgentWorkflowRunner(
    config,
    stream=WorkflowStream(),
    approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
    slash_commands=[
        *slash_commands.default_commands(),
        slash_commands.model_selector(
            choices=SUPPORTED_MODELS,
            set_model=lambda model: setattr(self, "_model", model),
            description="Set the model for this session.",
        ),
    ],
)
```

## Requirements

- Python **3.11+**
- [uv](https://docs.astral.sh/uv/) for dependency management
- [just](https://just.systems/) for the example recipes
- [pnpm](https://pnpm.io/) for building or developing the Svelte UI
- A Temporal service. The Monty example can start a local dev server with `just temporal`
  if you have the `temporal` CLI installed.

## Run The Example

The [`examples/monty`](examples/monty) example is the best end-to-end path: a
conversational travel agent and a subagent-driven variant, all built on Code Mode. First
create local environment settings at the **repo root** (one `.env.local` serves every
example):

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

These root recipes delegate into `examples/monty`, which reads the same repo-root
`.env.local`. You can also run the same recipes directly from `examples/monty`; there the
agent worker recipe is named `just worker`.

Open <http://localhost:8000> and select a Monty agent. `just server` runs
`app-build` first, so port 8000 serves the current built Svelte UI from
`temporal_agent_harness/ui/dist`, not the legacy static HTML files.

## Status & docs

This is experimental and under active development; expect breaking changes. Deeper design
documentation — the agent protocol, the streaming model, human-in-the-loop approvals, and
agents-as-subagents — lives under [`docs/internal/`](docs/internal). Contributor setup
(repository layout, the root `justfile`, UI development, and packaging) is in
[`docs/internal/development.md`](docs/internal/development.md).
