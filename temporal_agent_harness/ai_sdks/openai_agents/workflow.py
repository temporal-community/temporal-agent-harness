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

from temporalio import activity
from temporalio import workflow as temporal_workflow
from temporalio.common import Priority, RetryPolicy
from temporal_agent_harness.ai_sdks.openai_agents.sandbox._temporal_sandbox_client import (
    TemporalSandboxClient,
)
from temporalio.exceptions import ApplicationError, TemporalError
from temporalio.workflow import (
    ActivityCancellationType,
    ActivityConfig,
    VersioningIntent,
)

if typing.TYPE_CHECKING:
    from agents.mcp import MCPServer


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


REGISTER_MCP_SERVER_SIGNAL = "register_mcp_server"
DEREGISTER_MCP_SERVER_SIGNAL = "deregister_mcp_server"
LIST_REGISTERED_MCP_SERVERS_QUERY = "list_registered_mcp_servers"


class NexusMcpServerRegistry:
    """Per-workflow registry of Nexus-reachable MCP tool sources.

    Two ways to populate it:

    * A known, fixed tool set, declared once at workflow-definition time (symmetric to
      ``AgentWorkflowRunner``'s ``approval_policy_default``) -- no live registration call
      needed at all::

          nexus_mcp_server_registry(default_servers={"demo-tools": "demo-tools-endpoint"})

    * A genuinely dynamic registration, live from any client::

          await handle.signal(
              NexusMcpServerRegistry.REGISTER_MCP_SERVER_SIGNAL,
              args=["demo-tools", "demo-tools-endpoint"],
          )

      A SIGNAL, deliberately, not an update: a client typically registers a tool source
      immediately after starting a fresh conversation, and an update sent before its
      handler exists fails outright (confirmed live), whereas a signal is buffered by the
      server until one is registered -- so registration works regardless of how early it's
      sent, with no need to wait for the workflow to reach any particular point first. The
      trade-off is no synchronous ack/validation error; a caller that needs to confirm a
      registration landed (or was rejected -- an invalid one is logged and dropped, not
      applied) can follow up with the ``list_registered_mcp_servers`` query.

    Or call `register` directly from within the workflow.
    """

    def __init__(self, default_servers: Mapping[str, str] | None = None) -> None:
        self.servers: dict[str, str] = dict(default_servers or {})
        """Map storing `{nexus_service_name: nexus_endpoint}`, with a basic CRUD via signal handlers."""

        # Register signal/query handlers dynamically so the containing workflow doesn't
        # need to.
        temporal_workflow.set_signal_handler(
            REGISTER_MCP_SERVER_SIGNAL, self._handle_register
        )
        temporal_workflow.set_signal_handler(
            DEREGISTER_MCP_SERVER_SIGNAL, self._handle_deregister
        )
        temporal_workflow.set_query_handler(
            LIST_REGISTERED_MCP_SERVERS_QUERY, self._handle_list
        )

    def register(self, name: str, endpoint: str) -> None:
        """Register (or replace) a Nexus-reachable MCP tool source.

        Args:
            name: Must match that service's own actual Nexus service name — see the class
                docstring.
            endpoint: The Nexus endpoint name that reaches it.
        """
        self.servers[name] = endpoint
        temporal_workflow.logger.info(
            "[nexus-mcp-registry] registered %r -> %s", name, endpoint
        )

    def _handle_register(self, name: str, endpoint: str) -> None:
        if not name or not endpoint:
            temporal_workflow.logger.error(
                "[nexus-mcp-registry] dropping registration: both name and endpoint are "
                "required (got name=%r, endpoint=%r)", name, endpoint,
            )
            return
        # name is routed on by splitting a tool name on its FIRST underscore (see
        # WorkflowTransport._call_tool) -- an underscore in the service name itself makes
        # every one of its tools unroutable, so reject it here rather than at call time.
        # A signal can't reject synchronously (see the class docstring); logging and
        # dropping is the best this handler can do -- check
        # list_registered_mcp_servers to confirm a registration actually landed.
        with temporal_workflow.unsafe.imports_passed_through():
            try:
                from authoring import validate_service_name
            except ModuleNotFoundError:
                temporal_workflow.logger.error(
                    "[nexus-mcp-registry] dropping registration %r: the `nexus-mcp` "
                    "extra is not installed (uv sync --extra nexus-mcp)", name,
                )
                return
        try:
            validate_service_name(name)
        except ValueError as exc:
            temporal_workflow.logger.error(
                "[nexus-mcp-registry] dropping registration %r: %s", name, exc
            )
            return
        self.register(name, endpoint)

    def _handle_deregister(self, name: str) -> None:
        removed = self.servers.pop(name, None)
        if removed is not None:
            temporal_workflow.logger.info("[nexus-mcp-registry] deregistered %r", name)
        else:
            temporal_workflow.logger.debug(
                "[nexus-mcp-registry] deregister: %r not found (stale signal, ignoring)", name
            )

    def _handle_list(self) -> dict[str, str]:
        return dict(self.servers)


# Stashed on workflow.instance() (stable per execution) -- not a module global, which
# would leak across concurrently-running workflows on this worker.
_REGISTRY_INSTANCE_ATTR = "__temporal_agent_harness_nexus_mcp_registry"


def nexus_mcp_server_registry(
    default_servers: Mapping[str, str] | None = None,
) -> NexusMcpServerRegistry:
    """Return this workflow's `NexusMcpServerRegistry`, creating it on first use.

    One per workflow execution, shared by every caller. This allows us to only have
    a singleton of the registry of all Nexus MCP servers registerred to this run.

    Args:
        default_servers: `{nexus_service_name: nexus_endpoint}` entries to seed the
            registry with when creating it -- lets a workflow author declare a known,
            fixed tool set at workflow-definition time, with no live registration call
            needed for the common case. Ignored on every call after the first (the
            registry already exists by then).

    NOTE: Don't call from ``@workflow.init`` -- ``workflow.instance()`` isn't set yet there.
          Call from ``@workflow.run`` or a handler instead. Registration itself still works
          fine regardless -- ``register_mcp_server`` is a signal, buffered by the server
          until its handler exists, so a caller never needs to wait for this to have run.
    """
    instance = temporal_workflow.instance()
    registry = getattr(instance, _REGISTRY_INSTANCE_ATTR, None)
    if registry is None:
        registry = NexusMcpServerRegistry(default_servers)
        setattr(instance, _REGISTRY_INSTANCE_ATTR, registry)
    return registry


def nexus_transport_mcp_server(
    name: str | None = None,
    allowed_servers: frozenset[str] | None = None,
    **kwargs: Any,
) -> AbstractAsyncContextManager["MCPServer"]:
    """A durable MCP server backed by `nexus_mcp`'s `WorkflowTransport`: tool calls go
    through Nexus, against whatever's registered in this workflow's `nexus_mcp_server_registry`.
    By default every registered service is visible, immediately, no separate enable/restrict
    step -- pass `allowed_servers` to narrow that for one particular agent (see below).

    `OpenAIAgentsPlugin(nexus_mcp_initial_servers={...})` auto-injects one of these into EVERY
    agent (and handoff target) in the graph, all sharing the same unrestricted visibility.
    To give a specific agent a narrower slice of the registry instead, construct its own
    scoped instance directly and pass it via `Agent(mcp_servers=[...])` -- the plugin skips
    auto-injecting its own once an agent already carries one::

        restricted = nexus_transport_mcp_server(allowed_servers=frozenset({"weather-tools"}))
        agent = Agent(name="Weather", mcp_servers=[restricted])  # sees ONLY weather-tools,
                                                                  # even if other servers are
                                                                  # registered against this
                                                                  # workflow too.

    Requires the `nexus-mcp` package.

    Args:
        name: A readable name for the server. Defaults to `"nexus-transport"` if not provided.
        allowed_servers: If given, restricts this instance to only these registered names --
            everything else registered against the same workflow-wide registry stays
            invisible to it. `None` (the default) sees everything registered.
        **kwargs: Forwarded to `agents.mcp.MCPServer.__init__` (`require_approval`,
            `failure_error_function`, etc).

    Example:
        async with nexus_transport_mcp_server() as mcp_server:
            agent = Agent(name="Assistant", instructions="...", mcp_servers=[mcp_server])
            result = await Runner.run(agent, input=query)
    """
    from temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp import (
        _NexusTransportMCPServer,
    )

    return _NexusTransportMCPServer(
        nexus_mcp_server_registry().servers,
        name=name,
        allowed_servers=allowed_servers,
        **kwargs,
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
