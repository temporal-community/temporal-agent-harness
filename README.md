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
└── utils/        # general Temporal utilities (e.g. large-payload offload)

examples/
├── monty/            # a travel-booking agent example (start here)
└── session_manager/  # an agent-agnostic launcher + web chat UI the examples run on

tests/            # mirrors the package layout
```

## Try it

The [`examples/monty`](examples/monty) example is the best way to see the harness end to end — a
conversational travel agent, plus a variant that drives another agent as a subagent. Its
[README](examples/monty/README.md) walks through the one-time setup and the handful of `just`
commands to run the full stack locally.

## Requirements

- Python **3.11+**
- A Temporal service — a local dev server works out of the box (`just temporal` in the example)
- [uv](https://docs.astral.sh/uv/) for dependency management

## Status & docs

This is experimental and under active development; expect breaking changes. Deeper design
documentation — the agent protocol, the streaming model, human-in-the-loop approvals, and
agents-as-subagents — is being written and will land under [`docs/`](docs).
