# Strands Agents on the harness

Run a [Strands Agents](https://strandsagents.com/) agent as a harness agent. Every model call runs
as a Temporal activity and streams live onto the agent's turn: replies, reasoning, tool requests
and token usage. Every tool call goes through the harness: the approval policy, `tool_start` /
`tool_end` / `tool_error` events, `Injected` parameters, and mid-turn steering.

Two modules make up the integration:

- `temporal_agent_harness.ai_sdks.strands_agents`: the Temporal Strands plugin (`StrandsPlugin`,
  `TemporalAgent`, `TemporalSandbox`, …), vendored from `temporalio/ai-integrations` with a
  streaming seam added. See [Vendoring](#vendoring).
- `temporal_agent_harness.ai_sdks.strands_agents_harness`: the harness glue.
  `harness_strands_agent` builds the agent, `as_strands_tool` adapts harness tools, and
  `harness_observer_factory` streams model calls onto the turn.

`python/examples/strands_hello` is a complete agent using everything below.

> **Experimental.** This integration may change in future versions.

## Installation

```sh
uv add 'temporal-agent-harness[strands-agents]'
```

The extra brings `strands-agents`, `botocore`, and the MCP packages the vendored plugin imports.
Install the model provider you use too, e.g. `openai` for `strands.models.openai.OpenAIModel`.

## Quickstart

### Workflow

```python
from datetime import timedelta

from temporalio.contrib.workflow_streams import WorkflowStream

from temporal_agent_harness.ai_sdks.strands_agents import TemporalSandbox
from temporal_agent_harness.ai_sdks.strands_agents_harness import (
    as_strands_tool,
    harness_strands_agent,
)
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    MessageContext,
    MidTurn,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner, Injected


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str) -> str:
    """Return the current weather for a city."""
    return f"It's 72°F and sunny in {city}."


@agent.defn(name="WeatherAgent")
class WeatherAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._agent = harness_strands_agent(
            self._runner,
            model="openai",  # a name registered in StrandsPlugin(models=...)
            start_to_close_timeout=timedelta(minutes=2),
            system_prompt="You are a friendly assistant.",
            tools=[as_strands_tool(self._runner, get_weather)],
            sandbox=TemporalSandbox("scratch"),  # optional
            callback_handler=None,  # replies stream through the turn, not to stdout
        )

    @agent.accepts(mid_turn=MidTurn.ACCEPT)
    async def ask(self, message: TextMessage, ctx: Injected[MessageContext]) -> TextReply:
        """Chat with the assistant; sent mid-turn, the message steers it."""
        if ctx.joined_turn:
            self._runner.steer(message.text)
            return TextReply(text="")
        result = await self._agent.invoke_async(message.text)
        return TextReply(text=str(result))
```

Build the agent in the `@agent.init` method. It keeps the conversation in `agent.messages` for as
long as the workflow runs. Use `invoke_async` (or `stream_async`), never `agent(...)`: see
[Synchronous calls](#synchronous-calls).

### Worker

```python
from strands.models.openai import OpenAIModel
from strands.sandbox.docker import DockerSandbox

from temporal_agent_harness.ai_sdks.strands_agents import StrandsPlugin
from temporal_agent_harness.ai_sdks.strands_agents_harness import harness_observer_factory
from temporal_agent_harness.plugin import AgentHarnessPlugin

strands = StrandsPlugin(
    models={"openai": lambda: OpenAIModel(model_id="gpt-5.1")},
    sandboxes={"scratch": build_sandbox},  # see Sandboxes
    observer_factory=harness_observer_factory,
)
client = await Client.connect(..., plugins=[strands, AgentHarnessPlugin(tools=[...])])
worker = Worker(client, task_queue="weather", workflows=[WeatherAgent])
```

Put `StrandsPlugin` on the client, before `AgentHarnessPlugin`. Workers built from that client
pick both up. `StrandsPlugin` registers the model and sandbox activities and the data converter
Strands needs. `AgentHarnessPlugin(tools=[...])` registers the activity bodies of your
`@agent.activity_tool_defn` tools. Without `observer_factory=harness_observer_factory`, model
calls run but nothing streams onto the turn.

## Streaming

`harness_strands_agent` routes every model call to the turn in progress. Inside the model
activity, the harness observer translates the raw Strands stream:

| Strands stream event | Turn event |
|---|---|
| call starts | `model_interaction_started` (the worker model's `model_id`) |
| `contentBlockDelta.delta.text` | `reply_delta` |
| `contentBlockDelta.delta.reasoningContent` | `thought_summary` |
| a tool-use block (`toolUseId`, name, streamed input) | one `tool_requested`, keyed by `toolUseId` |
| `metadata.usage` | the usage on `model_interaction_ended`, which is always published |

Events are batched like other integrations' (100 ms by default). The workflow still receives
every event in the activity result, so replay never depends on the stream.

## Tools

Every tool call of a harness Strands agent gets the agent's approval policy, lifecycle events and
steering:

| Tool | Declare with | Runs |
|---|---|---|
| Workflow tool | `@agent.tool_defn` | inline, in the workflow |
| Activity tool | `@agent.activity_tool_defn` | on a worker, as a durable activity; register it with `AgentHarnessPlugin(tools=[...])` |
| Callback tool | `@agent.callback_tool_defn` | on a client: the call waits in the workflow until a client posts the result |
| MCP tools | `TemporalMCPClient(server)` in `tools` | one activity per call, against the MCP server the worker registers |
| Sandbox tools | `sandbox=TemporalSandbox(name)` | `sandbox_shell` and `sandbox_file_editor`, one activity per operation on the worker's sandbox |

Pass each declared tool through `as_strands_tool(runner, tool)` (or `as_strands_tools(runner,
[...])`). The model sees the tool's model-facing signature, so `Injected[...]` parameters stay
hidden. `tool_requested`, `tool_start` and `tool_end` share Strands' `toolUseId`.

MCP and sandbox tools run each call as an `@agent.tool_defn` tool named and described as the
Strands tool is, so they are gated and bracketed exactly like your own tools.

`harness_strands_agent` refuses anything else: plain Strands `@tool` functions, `strands_tools`,
`workflow.activity_as_tool`, and a `sandbox` that is not a `TemporalSandbox`. To use one of those capabilities, write it as a harness tool: for example an
`@agent.activity_tool_defn` that calls the `strands_tools` function.

### Injected parameters

A tool parameter annotated `Injected[...]` is filled by the workflow, not the model. It is left
out of the tool's schema, so the model never sees or supplies it. Pass the values, keyed by
parameter name, when you adapt the tool:

```python
from temporal_agent_harness.harness.agent_workflow import Injected


@agent.tool_defn(inherently_safe=True)
async def record_stop(city: str, board: Injected[TripBoard]) -> str:
    """Add a city to the trip."""
    board.add(city)
    return f"Added {city}."


# in @agent.init
self._board = TripBoard()
self._agent = harness_strands_agent(
    self._runner,
    model="openai",
    tools=[as_strands_tool(self._runner, record_stop, injections={"board": self._board})],
)
```

`as_strands_tools(runner, [...], injections={...})` gives every tool in the list the same mapping;
each tool takes only the names it declares. Every call hands the mapping to
`runner.run_tool(..., injections=...)`, which fills the parameters of workflow and activity tools
alike. An activity tool's injected values are serialized into its activity input like its other
arguments.

The mapping is bound when the agent is built, so inject long-lived values rather than per-turn
ones. A workflow tool can take any object, such as a state holder or the runner; an activity
tool's injected values must be serializable, since they travel in its activity input. If a tool
declares an injected parameter the mapping doesn't supply, its call fails and the model gets an
error tool result naming the missing argument.

## Human input and approvals

Strands pauses an agent for a person with *interrupts*: the run stops, and the caller resumes it
with answers, which runs the interrupted tool again. A harness agent uses the harness's own
mechanisms instead, which keep the turn running:

- **Ask a person or a client for something:** an `@agent.callback_tool_defn` tool. Its call
  publishes `callback_requested`, waits durably in the workflow (no activity, no timeout unless
  you set one), and returns the client's answer as the tool's result.
- **Approve a tool call:** the agent's `ToolApprovalPolicy`. Sandbox tools are not inherently
  safe, so `allow_inherently_safe()` sends each of their calls to a person.

`harness_strands_agent` refuses Strands interrupts. If a tool or a hook (for example a
`BeforeToolCallEvent` hook calling `event.interrupt(...)`) interrupts the run, the call fails with
a non-retryable `ApplicationError` of type `StrandsInterruptNotSupported`, the turn publishes
`message_handler_error`, and the turn ends.

## Mid-turn steering

Accept messages mid-turn (`@agent.accepts(mid_turn=MidTurn.ACCEPT)`) and, when
`ctx.joined_turn`, queue the text with `runner.steer(text)`, as in the quickstart. The harness
appends queued steering to the next tool result the model receives, from any of the tools above.
`tool_end` keeps the tool's own output, and the turn stream shows `steering_enqueued` then
`steering_claimed`.

## Models

`StrandsPlugin(models={name: factory})` maps names to model factories. Each factory runs lazily on
the worker, inside the model activity, and its model is cached for the worker's lifetime.
`harness_strands_agent(model=name)` selects one. Read credentials in the factory (or let the
provider read its own environment variable); never pass them from workflow code.

Strands' own model retries are off: `retry_strategy` is refused, and the default Bedrock model
turns Botocore's retries off. Configure retries with `retry_policy` and the other activity
options (`start_to_close_timeout`, `heartbeat_timeout`, `task_queue`, …) on
`harness_strands_agent`, and turn off retries in any provider client you build yourself.

Before an agent's first model call, a `strands_get_model_config` activity reads the worker
model's `model_id`, context window and whether it is stateful, so Strands' context management
sees the real model. A stateful model (`OpenAIResponsesModel(stateful=True)`, for example) cannot
be combined with a `conversation_manager`; the agent raises `ValueError` on its first call.

## MCP servers

Register each MCP server on the worker by name, and pass a `TemporalMCPClient` for it in the
agent's `tools`:

```python
from mcp import Client, StdioServerParameters

StrandsPlugin(
    models={...},
    mcp_clients={"f1-data": lambda: Client(StdioServerParameters(command="f1-mcp"))},
    observer_factory=harness_observer_factory,
)
```

```python
# in @agent.init
self._agent = harness_strands_agent(
    self._runner,
    model="openai",
    tools=[
        as_strands_tool(self._runner, get_weather),
        TemporalMCPClient("f1-data", cache_tools=True, start_to_close_timeout=timedelta(minutes=1)),
    ],
)
```

Listing the server's tools and each tool call are activities, so they replay from history. The
agent's approval policy covers every MCP call: under a policy that asks a person, each call waits
for approval, and a denied call reaches the model as an error result without reaching the server.
Address an MCP tool in the policy or in `AutoApprovalCriteria.tools` by the name the model sees
(with the client's `prefix`, if any). `cache_tools=True` lists the tools once per workflow instead
of before every model call.

## Sandboxes

`TemporalSandbox(name, <activity options>)` is Strands' sandbox API in the workflow: each command
and file operation is an activity, run on the worker against the sandbox that
`StrandsPlugin(sandboxes={name: factory})` builds. Strands includes `DockerSandbox` (an existing
container) and `SshSandbox` (any host you can reach over SSH, a VM for example); any other
`Sandbox` subclass works too. `NotASandboxLocalEnvironment` runs on the worker's own machine with
no isolation; the example uses it to avoid Docker.

```python
from strands.sandbox.docker import DockerSandbox
from temporal_agent_harness.ai_sdks.strands_agents import SandboxWorkflowContext

async def build_sandbox(context: SandboxWorkflowContext) -> DockerSandbox:
    # Idempotent: return this workflow's container if another worker already provisioned it.
    container = await get_or_create_container(context.chain.first_execution_run_id)
    return DockerSandbox(container.name)
```

What a factory must do:

- **Called for every operation.** The plugin caches nothing. Each call gets the run's
  `SandboxWorkflowContext`, and successive operations can land on different workers, so every
  worker must reach the same workflow-scoped environment. Key it on
  `context.chain.first_execution_run_id`, which survives retries, worker restarts and
  Continue-As-New.
- **Idempotent provisioning.** Calls can run concurrently. A synchronous factory must only build a
  lightweight adapter; use an async factory for lookup or provisioning. Return an async context
  manager to scope connections to one activity.
- **At-least-once operations.** A worker can finish a command and fail before recording it, so a
  retry runs it again. Give `TemporalSandbox` a bounded `retry_policy`. Reset does not undo
  sandbox side effects.
- **Mind the history.** Command output and file contents are activity results, recorded in history
  and subject to payload size limits.

Provisioning, teardown and reaping orphaned environments are yours.

## Synchronous calls

Strands runs its synchronous APIs on a separate thread, outside the workflow sandbox, where their
I/O would not be recorded. So in workflow code these raise `RestrictedWorkflowAccessError`:
calling the agent (`agent(...)`), `agent.structured_output()`, direct tool calls such as
`agent.tool.name()`, and Strands features that make such calls, such as
`SummarizingConversationManager`. The error fails the workflow task, which retries until you
deploy a fix. Use `invoke_async` / `stream_async`.

## Not available in a harness agent

These upstream features work only on a plain `TemporalAgent`, which is still exported: Strands
`@tool`s and `workflow.activity_as_tool`, interrupts, and raw `streaming_topic` streaming. A plain `TemporalAgent`
streams nothing onto a turn and its tools bypass the harness. See
[upstream's README at the vendored commit](https://github.com/temporalio/ai-integrations/blob/a18192838b2390ed2186d68018ff6adea657461a/python/strands_agents/README.md)
for those features.

## Vendoring

This package is `temporalio/ai-integrations` `python/strands_agents` at
`a18192838b2390ed2186d68018ff6adea657461a` (PyPI `temporalio-strands-agents` 0.1.0).

- **Imports:** `temporalio.strands_agents` imports, and upstream's relative imports, are rewritten
  to absolute `temporal_agent_harness.ai_sdks.strands_agents` ones. Gazelle derives Bazel deps only
  from absolute imports.
- **Local changes:** every local change is marked with a `# Harness:` comment. They are:
  - the streaming seam: `StrandsPlugin(observer_factory=...)` and `ModelActivity(observer_factory=...)`,
    `TemporalModel` / `TemporalAgent(stream_route=...)`, and a `stream_to` token on the streaming
    activity input;
  - the workflow-sandbox passthrough of this package (upstream gets it under `temporalio.*`);
  - install hints and `StrandsPlugin.version()` naming the harness distribution;
  - a `gazelle:ignore` for the Python 3.14-only `annotationlib` import.
- **Syncing upstream:** copy the new upstream source, repeat the import rewrite, and re-apply the
  marked hunks. Upstream's own test suite passes against this copy once its imports are rewritten
  the same way, except its package-version test (`version()` names the harness here).
