# Temporal Nexus MCP

The `nexus_mcp` package exposes Nexus service operations as MCP tools. It
supports direct OpenAI Agents and Pydantic AI integrations, Temporal workflow
calls, standard MCP clients, and MCP Tasks.

For component design, execution paths, and protocol behavior, see
[Architecture](ARCHITECTURE.md).

## Install the package

The package requires Python 3.13 or later. The distribution name is
`temporal-nexus-mcp`. The Python import name is `nexus_mcp`.

An unrelated project owns the `nexus-mcp` name on PyPI. Do not run
`pip install nexus-mcp` or `uv add nexus-mcp`.

From this repository, run:

```sh
uv sync --extra nexus-mcp
```

For an editable package-only installation, run:

```sh
uv pip install -e ./nexus/mcp
```

Install the optional dependency for the AI SDK that you use:

```sh
uv pip install -e './nexus/mcp[openai-agents]'
uv pip install -e './nexus/mcp[pydantic-ai]'
```

To install from GitHub before the package is published, run:

```sh
uv add "temporal-nexus-mcp @ git+https://github.com/temporal-community/temporal-agent-harness.git#subdirectory=nexus/mcp"
```

## Select an API

| Goal | API | Tool transport |
| --- | --- | --- |
| Add Nexus tools to an OpenAI agent outside a workflow | `NexusMCPServer` | Nexus |
| Add Nexus tools to Pydantic AI outside a workflow | `NexusToolset` | Nexus |
| Add Nexus tools to an OpenAI agent workflow | `nexus_native_mcp_server` | Nexus |
| Call Nexus tools from a workflow without an AI SDK | `NexusToolResolver` with `WorkflowNexusExecutor` | Nexus |
| Serve Nexus tools to a standard MCP client | `NexusMCPBridge` | stdio, then Nexus |
| Run long calls as MCP tasks | `NexusMCPBridge` with Tasks support | stdio, then Nexus |
| Call an existing HTTP MCP server through Temporal | Durable Tools Gateway | Nexus, then HTTP |

Use a native Nexus tool service when you control the tool implementation. Use
the Durable Tools Gateway when the tool is already available from an HTTP MCP
server.

## Author a native Nexus tool service

Inherit from `MCPOverNexusServiceHandler`. Add `@nexus_mcp_tool` to each short
operation that you want to expose.

```python
import nexusrpc.handler
from mcp.types import ToolAnnotations
from nexus_mcp.authoring import (
    MCPOverNexusServiceHandler,
    MCPToolConfig,
    nexus_mcp_tool,
)
from pydantic import BaseModel


class Forecast(BaseModel):
    city: str
    summary: str


@nexusrpc.handler.service_handler(name="weather")
class WeatherTools(MCPOverNexusServiceHandler):
    mcp_tool_defaults = MCPToolConfig(
        annotations=ToolAnnotations(read_only_hint=True)
    )

    @nexus_mcp_tool(title="Get a weather forecast")
    async def forecast(self, city: str) -> Forecast:
        return Forecast(city=city, summary="Clear")
```

The decorator creates the input and output schemas, the synchronous Nexus
operation, the public MCP name, and the exact route to the operation. It accepts
both `def` and `async def` methods. Use it only for work that can finish before
the Nexus handler deadline.

Use `MCPToolConfig` for service defaults. Values on `@nexus_mcp_tool` override
the defaults. Only marked operations appear in `tools/list`.

Service names and complete generated tool names must match
`[a-zA-Z0-9_-]{1,64}`.

### Define the Nexus operation directly

Use `@nexus_mcp_operation` when you need direct control of the input and output
types, operation name, operation context, or execution model. Put it above the
Nexus operation decorator.

```python
import nexusrpc.handler
import temporalio.nexus
from nexus_mcp.authoring import MCPOverNexusServiceHandler, nexus_mcp_operation


@nexusrpc.handler.service_handler(name="weather")
class WeatherTools(MCPOverNexusServiceHandler):
    @nexus_mcp_operation(title="Get a delayed forecast")
    @temporalio.nexus.workflow_run_operation
    async def delayed_forecast(
        self,
        ctx: temporalio.nexus.WorkflowRunOperationContext,
        input: DelayedForecastInput,
    ) -> temporalio.nexus.WorkflowHandle[DelayedForecastOutput]:
        return await ctx.start_workflow(
            DelayedForecastWorkflow.run,
            input,
            id=f"delayed-forecast-{ctx.request_id}",
        )
```

The Nexus decorator defines the operation contract and execution behavior.
`@nexus_mcp_operation` exposes the operation as an MCP tool and adds MCP
metadata. The operation can be synchronous or workflow-backed. See the
[Nexus hello service](../../examples/nexus_hello/nexus_tool_service.py) for a
complete workflow-backed example.

Register the service handler and its workflows on a Temporal worker. Create a
Nexus endpoint that targets the worker task queue.

## Add Nexus tools to an AI SDK

The OpenAI Agents and Pydantic AI integrations run in the caller process. They
call Nexus directly and do not start an MCP protocol server. The Temporal
server must enable standalone Nexus operations for these integrations. For a
local Temporal server, set `nexusoperation.enableStandalone=true`.

### OpenAI Agents outside a workflow

Pass a connected Temporal client to
[`NexusMCPServer`](nexus_mcp/integrations/openai_agents.py). Add the server to
`Agent.mcp_servers`.

```python
from agents import Agent, Runner
from nexus_mcp.integrations.openai_agents import NexusMCPServer
from temporalio.client import Client

temporal = await Client.connect("localhost:7233")
nexus_tools = NexusMCPServer.for_service(
    temporal,
    "weather",
    "weather-endpoint",
)
agent = Agent(name="weather-agent", mcp_servers=[nexus_tools])
result = await Runner.run(agent, "What is the forecast for Boston?")
```

The caller owns the Temporal client. The adapter's `connect()` and `cleanup()`
methods do not start or stop a process.

Use the mapping constructor to expose more than one service:

```python
nexus_tools = NexusMCPServer(
    temporal,
    {
        "weather": "weather-endpoint",
        "calendar": "calendar-endpoint",
    },
)
```

The optional `request_context_factory` callback receives MCP `_meta`, or `None`
during tool discovery. By default,
`_meta["io.temporal/idempotencyKey"]` sets the Nexus idempotency key.

### OpenAI Agents in an Agent Harness workflow

Use the Agent Harness
[`nexus_native_mcp_server`](../../temporal_agent_harness/ai_sdks/openai_agents/workflow.py)
factory inside a Temporal workflow:

```python
from agents import Agent
from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
    nexus_native_mcp_server,
)

agent = Agent(
    name="weather-agent",
    mcp_servers=[nexus_native_mcp_server("weather", "weather-endpoint")],
)
```

The factory creates `WorkflowNexusMCPServer`, which uses
`WorkflowNexusExecutor`.

### Pydantic AI outside a workflow

Pass a connected Temporal client to
[`NexusToolset`](nexus_mcp/integrations/pydantic_ai.py). Add the toolset to
`Agent.toolsets`.

```python
from nexus_mcp.integrations.pydantic_ai import NexusToolset
from pydantic_ai import Agent
from temporalio.client import Client

temporal = await Client.connect("localhost:7233")
nexus_tools = NexusToolset.for_service(
    temporal,
    "weather",
    "weather-endpoint",
)
agent = Agent("openai:gpt-5.1", toolsets=[nexus_tools])
result = await agent.run("What is the forecast for Boston?")
```

Tool errors become `ModelRetry` errors. The optional
`request_context_factory` receives the Pydantic AI run context, tool name, and
tool arguments. The tool name and arguments are `None` during discovery.

## Call tools directly from a workflow

Use `NexusToolResolver` with `WorkflowNexusExecutor` when you do not use an AI
SDK adapter.

```python
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from nexus_mcp.execution import WorkflowNexusExecutor
    from nexus_mcp.resolver import NexusToolResolver


@workflow.defn
class WeatherWorkflow:
    @workflow.run
    async def run(self, city: str) -> dict[str, object] | None:
        tools = NexusToolResolver(
            {"weather": "weather-endpoint"},
            WorkflowNexusExecutor(),
        )
        result = await tools.call_tool("weather_forecast", {"city": city})
        return result.structured_content
```

Use `allowed_servers` to expose only part of a shared service map.

## Serve a standard MCP client

Use `NexusMCPBridge` when the caller requires an MCP protocol server. The
bridge accepts MCP requests over stdio and sends discovery and tool calls
through Nexus.

```python
from nexus_mcp.execution import StandaloneNexusExecutor
from nexus_mcp.frontends import NexusMCPBridge
from nexus_mcp.resolver import NexusToolResolver
from temporalio.client import Client

temporal = await Client.connect("localhost:7233")
resolver = NexusToolResolver(
    {"weather": "weather-endpoint"},
    StandaloneNexusExecutor(temporal),
)
bridge = NexusMCPBridge(resolver, name="weather-over-nexus", version="1.0.0")
await bridge.run_stdio_async()
```

The MCP client starts this module as a local stdio server. A local Temporal
server must set `nexusoperation.enableStandalone=true`.

## Use MCP Tasks

MCP Tasks are available through `NexusMCPBridge` when it uses
`StandaloneNexusExecutor`. Configure the client for protocol version
`2026-07-28` and add `NexusTasksClientExtension`.

```python
from mcp import StdioServerParameters
from mcp.client import Client
from nexus_mcp import NexusTasksClientExtension
from nexus_mcp.tasks import CreateTaskResult, get_task

stdio_server = StdioServerParameters(
    command="python",
    args=["weather_bridge.py"],
)

async with Client(
    stdio_server,
    mode="2026-07-28",
    extensions=[NexusTasksClientExtension()],
) as client:
    task = await client.session.call_tool(
        "weather_delayed_forecast",
        {"city": "New York", "delay_seconds": 5},
        allow_claimed=True,
    )
    assert isinstance(task, CreateTaskResult)
    current = await get_task(client.session, task.task_id)
```

The direct OpenAI Agents and Pydantic AI integrations wait for the Nexus
operation result. They do not expose the MCP Tasks protocol. See
[MCP Tasks architecture](ARCHITECTURE.md#mcp-tasks) for the task lifecycle and
failure behavior.

## Proxy an existing MCP server

Use the Durable Tools Gateway for an existing HTTP MCP server that cannot
become a native Nexus service.

```python
from agents import Agent
from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
    nexus_tools_gateway,
)

gateway = nexus_tools_gateway(agent_id="weather-agent")
agent = Agent(
    name="weather-agent",
    mcp_servers=[gateway.mcp_servers("weather")],
)
```

The gateway path is separate from the native Nexus tool path. See the
[Nexus hello example](../../examples/nexus_hello/README.md) and the
[gateway architecture](ARCHITECTURE.md#durable-tools-gateway).

## Further reading

- [Package architecture](ARCHITECTURE.md)
- [Nexus hello example](../../examples/nexus_hello/README.md)
- [Authoring helpers](nexus_mcp/authoring/authoring_helpers.py)
- [OpenAI Agents integration](nexus_mcp/integrations/openai_agents.py)
- [Pydantic AI integration](nexus_mcp/integrations/pydantic_ai.py)
