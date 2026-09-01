"""Workflow-specific primitives for working with the OpenAI Agents SDK in a workflow context"""

import functools
import inspect
import json
import typing
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import timedelta
from typing import Any

import nexusrpc
from agents import (
    RunContextWrapper,
    Tool,
)
from agents.function_schema import function_schema
from agents.tool import (
    FunctionTool,
)
from agents.tool_context import ToolContext

from temporalio import activity
from temporalio import workflow as temporal_workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.exceptions import ApplicationError, TemporalError
from temporalio.workflow import (
    ActivityCancellationType,
    ActivityConfig,
    VersioningIntent,
)

from temporal_agent_harness.ai_sdks.openai_agents.sandbox._temporal_sandbox_client import (
    TemporalSandboxClient,
)
from temporal_agent_harness.harness.agent_workflow import ToolApprovalDenied

if typing.TYPE_CHECKING:
    from agents.mcp import MCPServer

    from temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp import NexusGateway


def activity_as_tool(
    fn: Callable,
    *,
    task_queue: str | None = None,
    schedule_to_close_timeout: timedelta | None = None,
    schedule_to_start_timeout: timedelta | None = None,
    start_to_close_timeout: timedelta | None = None,
    heartbeat_timeout: timedelta | None = None,
    retry_policy: RetryPolicy | None = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: str | None = None,
    versioning_intent: VersioningIntent | None = None,
    summary: str | None = None,
    priority: Priority = Priority.default,
    strict_json_schema: bool = True,
) -> Tool:
    """Convert a single Temporal activity function to an OpenAI agent tool.

    This function takes a Temporal activity function and converts it into an
    OpenAI agent tool that can be used by the agent to execute the activity
    during workflow execution. The tool will automatically handle the conversion
    of inputs and outputs between the agent and the activity. Note that if you take a context,
    mutation will not be persisted, as the activity may not be running in the same location.

    For undocumented arguments, refer to :py:mod:`workflow` and :py:meth:`start_activity`

    Args:
        fn: A Temporal activity function to convert to a tool.
        strict_json_schema: Whether the tool should follow a strict schema.
            See https://openai.github.io/openai-agents-python/ref/tool/#agents.tool.FunctionTool.strict_json_schema


    Returns:
        An OpenAI agent tool that wraps the provided activity.

    Raises:
        ApplicationError: If the function is not properly decorated as a Temporal activity.

    Example:
        >>> @activity.defn
        >>> def process_data(input: str) -> str:
        ...     return f"Processed: {input}"
        >>>
        >>> # Create tool with custom activity options
        >>> tool = activity_as_tool(
        ...     process_data,
        ...     start_to_close_timeout=timedelta(seconds=30),
        ...     retry_policy=RetryPolicy(maximum_attempts=3),
        ...     heartbeat_timeout=timedelta(seconds=10)
        ... )
        >>> # Use tool with an OpenAI agent
    """
    ret = activity._Definition.from_callable(fn)
    if not ret:
        raise ApplicationError(
            "Bare function without tool and activity decorators is not supported",
            "invalid_tool",
        )
    if ret.name is None:
        raise ApplicationError(
            "Input activity must have a name to be made into a tool",
            "invalid_tool",
        )
    # If the provided callable has a first argument of `self`, partially apply it with the same metadata
    # The actual instance will be picked up by the activity execution, the partially applied function will never actually be executed
    params = list(inspect.signature(fn).parameters.keys())
    if len(params) > 0 and params[0] == "self":
        partial = functools.partial(fn, None)
        setattr(partial, "__name__", fn.__name__)
        partial.__annotations__ = getattr(fn, "__annotations__")
        setattr(
            partial,
            "__temporal_activity_definition",
            getattr(fn, "__temporal_activity_definition"),
        )
        partial.__doc__ = fn.__doc__
        fn = partial
    schema = function_schema(fn)

    async def run_activity(ctx: RunContextWrapper[Any], input: str) -> Any:
        try:
            json_data = json.loads(input)
        except Exception as e:
            raise ApplicationError(
                f"Invalid JSON input for tool {schema.name}: {input}"
            ) from e

        # Activities don't support keyword only arguments, so we can ignore the kwargs_dict return
        args, _ = schema.to_call_args(schema.params_pydantic_model(**json_data))

        # Add the context to the arguments if it takes that
        if schema.takes_context:
            args = [ctx] + args
        result = await temporal_workflow.execute_activity(
            ret.name,  # type: ignore
            args=args,
            task_queue=task_queue,
            schedule_to_close_timeout=schedule_to_close_timeout,
            schedule_to_start_timeout=schedule_to_start_timeout,
            start_to_close_timeout=start_to_close_timeout,
            heartbeat_timeout=heartbeat_timeout,
            retry_policy=retry_policy,
            cancellation_type=cancellation_type,
            activity_id=activity_id,
            versioning_intent=versioning_intent,
            summary=summary or schema.description,
            priority=priority,
        )
        try:
            return str(result)
        except Exception as e:
            raise ToolSerializationError(
                "You must return a string representation of the tool output, or something we can call str() on"
            ) from e

    return FunctionTool(
        name=schema.name,
        description=schema.description or "",
        params_json_schema=schema.params_json_schema,
        on_invoke_tool=run_activity,
        strict_json_schema=strict_json_schema,
    )


def nexus_operation_as_tool(
    operation: nexusrpc.Operation[Any, Any],
    *,
    service: type[Any],
    endpoint: str,
    schedule_to_close_timeout: timedelta | None = None,
    strict_json_schema: bool = True,
) -> Tool:
    """Convert a Nexus operation into an OpenAI agent tool.

    This function takes a Nexus operation and converts it into an
    OpenAI agent tool that can be used by the agent to execute the operation
    during workflow execution. The tool will automatically handle the conversion
    of inputs and outputs between the agent and the operation.

    Args:
        operation: A Nexus operation to convert into a tool.
        service: The Nexus service class that contains the operation.
        endpoint: The Nexus endpoint to use for the operation.
        strict_json_schema: Whether the tool should follow a strict schema

    Returns:
        An OpenAI agent tool that wraps the provided operation.

    Example:
        >>> @nexusrpc.service
        ... class WeatherService:
        ...     get_weather_object_nexus_operation: nexusrpc.Operation[WeatherInput, Weather]
        >>>
        >>> # Create tool with custom activity options
        >>> tool = nexus_operation_as_tool(
        ...     WeatherService.get_weather_object_nexus_operation,
        ...     service=WeatherService,
        ...     endpoint="weather-service",
        ... )
        >>> # Use tool with an OpenAI agent
    """

    def operation_callable(input: Any):  # type: ignore[reportUnusedParameter]
        raise NotImplementedError("This function definition is used as a type only")

    operation_callable.__annotations__ = {
        "input": operation.input_type,
        "return": operation.output_type,
    }
    operation_callable.__name__ = operation.name

    schema = function_schema(operation_callable)

    async def run_operation(_ctx: RunContextWrapper[Any], input: str) -> Any:
        try:
            json_data = json.loads(input)
        except Exception as e:
            raise ApplicationError(
                f"Invalid JSON input for tool {schema.name}: {input}"
            ) from e

        nexus_client = temporal_workflow.create_nexus_client(
            service=service, endpoint=endpoint
        )
        args, _ = schema.to_call_args(schema.params_pydantic_model(**json_data))
        assert len(args) == 1, "Nexus operations must have exactly one argument"
        [arg] = args
        result = await nexus_client.execute_operation(
            operation,
            arg,
            schedule_to_close_timeout=schedule_to_close_timeout,
        )
        try:
            return str(result)
        except Exception as e:
            raise ToolSerializationError(
                "You must return a string representation of the tool output, or something we can call str() on"
            ) from e

    return FunctionTool(
        name=schema.name,
        description=schema.description or "",
        params_json_schema=schema.params_json_schema,
        on_invoke_tool=run_operation,
        strict_json_schema=strict_json_schema,
    )


def harness_tool_as_openai_tool(fn: Callable, *, strict_json_schema: bool = True) -> Tool:
    """Convert an inline harness tool into an OpenAI agent tool.

    Calls use ``AgentWorkflowRunner.run_tool``. This preserves approval checks and tool
    events. Pass the active runner as the OpenAI ``Runner`` context.

    Args:
        fn: An inline harness tool.
        strict_json_schema: Use a strict tool schema when true.

    Returns:
        An OpenAI agent tool that wraps the provided harness tool.

    Example:
        >>> research_tools = agent.nexus_native_subagent(cls, endpoint, key="research")
        >>> sdk_agent = OpenAIAgent(
        ...     ...,
        ...     tools=[harness_tool_as_openai_tool(fn) for fn in research_tools],
        ... )
        >>> Runner.run_streamed(sdk_agent, input=..., context=self._runner)
    """
    schema = function_schema(fn)

    async def run_inline_tool(ctx: ToolContext[Any], input: str) -> Any:
        try:
            json_data = json.loads(input)
        except Exception as e:
            raise ApplicationError(
                f"Invalid JSON input for tool {schema.name}: {input}"
            ) from e

        args, kwargs = schema.to_call_args(schema.params_pydantic_model(**json_data))
        if schema.takes_context:
            args = [ctx] + args
        try:
            result = await ctx.context.run_tool(ctx.tool_call_id, fn, *args, **kwargs)
        except ToolApprovalDenied as exc:
            # A human rejection is a normal tool outcome, not a failed agent turn. Feed it
            # back to the model so it can respect the decision and finish its response.
            return f"Tool {schema.name!r} was not run: {exc}"
        try:
            return str(result)
        except Exception as e:
            raise ToolSerializationError(
                "You must return a string representation of the tool output, or something we can call str() on"
            ) from e

    return FunctionTool(
        name=schema.name,
        description=schema.description or "",
        params_json_schema=schema.params_json_schema,
        on_invoke_tool=run_inline_tool,
        strict_json_schema=strict_json_schema,
    )


def temporal_sandbox_client(
    name: str,
    config: ActivityConfig | None = None,
) -> Any:
    """Create a sandbox client reference for use in a Temporal workflow ``RunConfig``.

    .. warning::
        This is experimental and may change in future versions.
        Use with caution in production environments.

    This returns a ``BaseSandboxClient`` that dispatches all sandbox operations
    as Temporal activities, targeting the ``SandboxClientProvider`` registered
    on the worker with the matching ``name``.

    Example::

        run_config = RunConfig(
            sandbox=SandboxRunConfig(
                client=temporal_sandbox_client("daytona"),
                options=DaytonaSandboxClientOptions(...),
            ),
        )

    Args:
        name: The name of the ``SandboxClientProvider`` registered on the
            worker.  Must match exactly.
        config: Optional activity configuration for controlling timeouts,
            retries, etc.  Defaults to a 5-minute ``start_to_close_timeout``.
    """
    return TemporalSandboxClient(name=name, config=config)


def stateless_mcp_server(
    name: str,
    config: ActivityConfig | None = None,
    cache_tools_list: bool = False,
    factory_argument: Any | None = None,
) -> "MCPServer":
    """A stateless MCP server implementation for Temporal workflows.

    This uses a TemporalMCPServer of the same name registered with the OpenAIAgents plugin to implement
    durable MCP operations statelessly.

    This approach is suitable for simple use cases where connection overhead is acceptable
    and you don't need to maintain state between operations. It should be preferred to stateful when possible due to its
    superior durability guarantees.

    Args:
        name: A string name for the server. Should match that provided in the plugin.
        config: Optional activity configuration for MCP operation activities.
               Defaults to 1-minute start-to-close timeout.
        cache_tools_list: If true, the list of tools will be cached for the duration of the server
        factory_argument: Optional argument to be provided to the factory when producing an MCPServer
    """
    from temporal_agent_harness.ai_sdks.openai_agents._mcp import (
        _StatelessMCPServerReference,
    )

    return _StatelessMCPServerReference(
        name, config, cache_tools_list, factory_argument
    )


def stateful_mcp_server(
    name: str,
    config: ActivityConfig | None = None,
    server_session_config: ActivityConfig | None = None,
    factory_argument: Any | None = None,
) -> AbstractAsyncContextManager["MCPServer"]:
    """A stateful MCP server implementation for Temporal workflows.

    This wraps an MCP server to maintain a persistent connection throughout
    the workflow execution. It creates a dedicated worker that stays connected to
    the MCP server and processes operations on a dedicated task queue.

    This approach is more efficient for workflows that make multiple MCP calls,
    as it avoids connection overhead, but requires more resources to maintain
    the persistent connection and worker.

    The caller will have to handle cases where the dedicated worker fails, as Temporal is
    unable to seamlessly recreate any lost state in that case.

    Args:
        name: A string name for the server. Should match that provided in the plugin.
        config: Optional activity configuration for MCP operation activities.
               Defaults to 1-minute start-to-close and 30-second schedule-to-start timeouts.
        server_session_config: Optional activity configuration for the connection activity.
                       Defaults to 1-hour start-to-close timeout.
        factory_argument: Optional argument to be provided to the factory when producing an MCPServer
    """
    from temporal_agent_harness.ai_sdks.openai_agents._mcp import (
        _StatefulMCPServerReference,
    )

    return _StatefulMCPServerReference(
        name, config, server_session_config, factory_argument
    )


def nexus_native_mcp_server(
    name: str,
    endpoint: str,
    **kwargs: Any,
) -> "MCPServer":
    """OpenAI Agents MCP adapter for one native Nexus tool service.

    Pass the result directly to ``Agent(mcp_servers=[...])``.

    The service identified by `name` and `endpoint` must be a native Nexus tool
    service that is reachable with the provided `name` and `endpoint`.

    Requires the `nexus-mcp` package.

    Args:
        name: The service's real Nexus service name.
        endpoint: The Nexus endpoint name that reaches it.
        **kwargs: Forwarded to agents.mcp.MCPServer.__init__.

    Example:
        # Use the "demo-nexus" service at the "nexus-hello-demo-endpoint" endpoint.
        Agent(mcp_servers=[
            nexus_native_mcp_server("demo-nexus", "nexus-hello-demo-endpoint"),
        ])
    """
    from temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp import (
        _NexusNativeMCPServer,
    )

    return _NexusNativeMCPServer({name: endpoint}, name=name, **kwargs)


def nexus_gateway(
    account_id: str,
    *,
    gateway_name: str = "RegistryService",
    gateway_endpoint: str = "mcp-registry-endpoint",
) -> "NexusGateway":
    """A handle on the Durable Tools Gateway's 3rd-party servers registered for one
    account_id. This gateway will proxy calls between the agent and the registered servers.

    Not an MCPServer itself. Call .mcp_servers(*aliases) to get one, scoped to whichever
    registered aliases you pick -- see example usage.

    Requires the `nexus-mcp` package and a Durable Tools Gateway worker running at
    gateway_endpoint.

    Args:
        account_id: The owning account. Authentication will eventually supply this;
                    callers must pass it explicitly for now.
        gateway_name: The gateway's Nexus service name.
        gateway_endpoint: The Nexus endpoint name that reaches the gateway.

    Example:
        gateway = nexus_gateway("account-123")
        Agent(mcp_servers=[
            gateway.mcp_servers("foo-mcp", "bar-mcp"),
        ])
    """
    from temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp import NexusGateway

    if not account_id.strip():
        raise ValueError("account_id is required")
    return NexusGateway(
        account_id,
        gateway_name=gateway_name,
        gateway_endpoint=gateway_endpoint,
    )


class ToolSerializationError(TemporalError):
    """Error that occurs when a tool output could not be serialized.

    This exception is raised when a tool (created from an activity or Nexus operation)
    returns a value that cannot be properly serialized for use by the OpenAI agent.
    All tool outputs must be convertible to strings for the agent to process them.

    The error typically occurs when:
    - A tool returns a complex object that doesn't have a meaningful string representation
    - The returned object cannot be converted using str()
    - Custom serialization is needed but not implemented

    Example:
        >>> @activity.defn
        >>> def problematic_tool() -> ComplexObject:
        ...     return ComplexObject()  # This might cause ToolSerializationError

    To fix this error, ensure your tool returns string-convertible values or
    modify the tool to return a string representation of the result.
    """


class AgentsWorkflowError(TemporalError):
    """Error that occurs when the agents SDK raises an error which should terminate the calling workflow or update."""
