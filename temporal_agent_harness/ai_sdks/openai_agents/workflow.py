"""Workflow-specific primitives for working with the OpenAI Agents SDK in a workflow context"""

import functools
import inspect
import json
import typing
from collections.abc import Callable
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


class NexusMcpServerRegistry:
    """Per-agent-workflow live registry of every Nexus-reachable MCP tool source — Nexus-native
    servers and proxies (like the Durable Tools Gateway) alike, registered exactly the same
    way: a name and the Nexus endpoint that reaches it. Nothing else to declare.

    The *name* must be whatever that service's own actual Nexus service name is (the same
    requirement a Nexus-native server already has — e.g. ``"demo-tools"`` for tools named
    ``demo-tools_*``, or ``durable_tools_gateway.REGISTRY_SERVICE_NAME`` for the gateway).
    ``WorkflowTransport`` figures out on its own, from what each registered entry's
    ``list_tools`` actually returns, whether it's serving its own tools directly (one static
    Nexus operation per tool) or proxying tools registered on someone else's behalf (a
    generic ``list_tools``/``call_tool(name, args)`` pair, for a tool set it can't know ahead
    of time) — nothing about that distinction needs declaring at registration time.

    There is exactly one of these per agent workflow execution, managed automatically — see
    :func:`nexus_mcp_server_registry`. You should not normally need to construct this
    directly; it exists as a public type mainly so ``isinstance``/type-hint usages have
    something to reference.

    Any caller with a client connected to this workflow's own namespace — e.g. a Nexus-native
    server's own startup script, the Durable Tools Gateway's own worker, or an operator/demo
    script — registers with a plain signal, the same way any other client-facing action on an
    agent workflow works elsewhere in this harness::

        await handle.signal(
            NexusMcpServerRegistry.REGISTER_MCP_SERVER_SIGNAL,
            args=["demo-tools", "demo-tools-endpoint"],
        )

    No Nexus round trip needed for registration itself — only registered *servers'* tool
    calls go through Nexus. A workflow can also call :meth:`register` directly (e.g. to
    pre-wire a default in its own ``@workflow.init``) rather than waiting on an external
    signal.
    """

    REGISTER_MCP_SERVER_SIGNAL = REGISTER_MCP_SERVER_SIGNAL
    DEREGISTER_MCP_SERVER_SIGNAL = DEREGISTER_MCP_SERVER_SIGNAL

    def __init__(self) -> None:
        self.servers: dict[str, str] = {}
        """Live ``{name: nexus_endpoint}`` map, mutated in place by the signal handlers below
        (or by :meth:`register`). Pass this directly to ``nexus_transport_mcp_server``/
        ``WorkflowTransport`` — it stays live without needing its own wrapper type."""

        # Register signal handlers dynamically so the containing workflow doesn't need to.
        temporal_workflow.set_signal_handler(
            REGISTER_MCP_SERVER_SIGNAL, self._handle_register
        )
        temporal_workflow.set_signal_handler(
            DEREGISTER_MCP_SERVER_SIGNAL, self._handle_deregister
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
        self.register(name, endpoint)

    def _handle_deregister(self, name: str) -> None:
        removed = self.servers.pop(name, None)
        if removed is not None:
            temporal_workflow.logger.info("[nexus-mcp-registry] deregistered %r", name)
        else:
            temporal_workflow.logger.debug(
                "[nexus-mcp-registry] deregister: %r not found (stale signal, ignoring)", name
            )


# Stashed as a plain attribute on workflow.instance() (the workflow's own class instance,
# which the Temporal SDK guarantees is one single, stable object for a given workflow
# execution's entire lifetime) — NOT a module-level global, since a global would be shared
# across every concurrently-running workflow on this worker. This is the same mechanism
# TemporalOpenAIRunner uses to find "this workflow's" registry for automatic MCP server
# injection (see _openai_runner.py) even though the runner object itself carries no
# per-workflow state — confirmed live, including under concurrent workflow execution, before
# relying on it here.
_REGISTRY_INSTANCE_ATTR = "_temporal_agent_harness_nexus_mcp_registry"


def nexus_mcp_server_registry() -> NexusMcpServerRegistry:
    """Return the current workflow's :class:`NexusMcpServerRegistry`, creating it on first use.

    There is exactly one per agent workflow execution, regardless of how many times or from
    where this is called — :func:`nexus_transport_mcp_server` and ``OpenAIAgentsPlugin``'s
    automatic MCP-server injection (when configured with ``nexus_transport=True``) both call
    this internally, so a server registered via a signal is visible to both. Call this
    yourself only if you want to inspect or explicitly pass around the registry (e.g. in a
    status query); you never need to construct :class:`NexusMcpServerRegistry` directly.

    .. important::
        Do not call this from ``@workflow.init`` (``__init__``) — the Temporal SDK doesn't
        set ``workflow.instance()`` (which this needs) until *after* construction completes,
        so it raises ``AttributeError: 'NoneType' object has no attribute ...`` at that point
        (confirmed live). Call it from ``@workflow.run`` (or any handler) instead, e.g. to
        pre-register a default server before starting the harness's own turn loop.
    """
    instance = temporal_workflow.instance()
    registry = getattr(instance, _REGISTRY_INSTANCE_ATTR, None)
    if registry is None:
        registry = NexusMcpServerRegistry()
        setattr(instance, _REGISTRY_INSTANCE_ATTR, registry)
    return registry


# Distinguishes "caller didn't pass enabled_services at all" (-> enforce the harness's own
# opt-in policy, reading the current AgentWorkflowRunner) from "caller explicitly passed
# None" (-> disable filtering entirely, e.g. for a bare workflow with no AgentWorkflowRunner
# at all) — None itself is a meaningful value here, so it can't double as "not provided".
_ENABLED_SERVICES_UNSET: Any = object()


def nexus_transport_mcp_server(
    name: str | None = None,
    *,
    enabled_services: "Callable[[], frozenset[str]] | None" = _ENABLED_SERVICES_UNSET,
    **kwargs: Any,
) -> AbstractAsyncContextManager["MCPServer"]:
    """A durable MCP server that reaches tools through Temporal Nexus.

    Unlike :func:`stateless_mcp_server` / :func:`stateful_mcp_server`, this strategy calls
    straight into ``nexus_mcp``'s ``WorkflowTransport`` in-process, in the workflow's own
    event loop — no dedicated worker, no session, nothing to connect. All real I/O is still
    routed through Nexus, uniformly, against whatever is registered live against this
    workflow's own :func:`nexus_mcp_server_registry` — there is no separate, worker-level
    "gateway" concept at all:

      - A Nexus-native MCP server's own tools are called directly
        (``workflow.create_nexus_client()`` against its own endpoint) — no proxy, no
        activity, one Temporal hop.
      - A tool prefix that doesn't match any directly-registered server instead falls back to
        whichever registered entry is proxying tools registered on someone else's behalf
        (e.g. the Durable Tools Gateway), which starts a plain workflow wrapping one activity to reach the 3rd-party
        server on the caller's behalf.

    ``WorkflowTransport`` figures out which of the two applies on its own, from what each
    registered entry's ``list_tools`` actually returns — there's nothing to declare at
    registration time. Both live in ``nexus_mcp``'s ``WorkflowTransport`` — this factory just
    plugs that transport into an ``agents.mcp.MCPServer``. Registration is entirely self-serve
    and per-workflow-execution — see :class:`NexusMcpServerRegistry`; this factory does not
    register anything itself.

    You will not normally call this directly — configure ``OpenAIAgentsPlugin`` with
    ``nexus_transport=True`` instead, and every ``Agent`` gets one of these automatically,
    with no ``mcp_servers=[...]`` wiring needed at the call site at all. Call this yourself
    only if you need a *second*, differently-configured transport (e.g. a different
    ``enabled_services`` policy, or ``require_approval``).

    Requires the ``nexus-mcp`` extra (``uv sync --extra nexus-mcp``), which is only
    resolvable from an editable checkout of this repo and requires Python >=3.13.

    .. important::
        The calling workflow must be declared ``@workflow.defn(sandboxed=False)``. Nothing
        here opens a real session or anyio task group, but ``agents.mcp.server`` (home of
        the ``MCPServer`` base class this implements) still does ``import anyio`` at module
        scope, and re-executing that inside Temporal's sandboxed workflow runner hangs
        rather than raising a clean error — confirmed empirically, see ``_nexus_mcp.py``'s
        module docstring. This mirrors ``nexus_mcp``'s own ``ToolCallWorkflow`` /
        ``ToolRegistryWorkflow``, which are themselves declared ``sandboxed=False``. Using
        this inside a sandboxed workflow raises a clear ``ApplicationError`` at construction
        time instead.

    Args:
        name: A readable name for the server. Defaults to ``"nexus-transport"``.
        enabled_services: Which service names are available — reachability alone (a
            registered Nexus-native server, or a service a registered proxy knows about) is
            never enough; a service must also be *enabled*. Defaults to enforcing the
            calling workflow's own opt-in list (``AgentWorkflowRunner.enabled_mcp_servers``,
            itself sourced from ``AgentConfig.enabled_mcp_servers`` — empty unless a caller
            or the agent's own default says otherwise), read live via
            ``temporal_agent_harness.harness.agent_workflow.current_agent_workflow_runner``.
            Pass ``None`` explicitly to disable filtering entirely (e.g. a bare workflow
            with no ``AgentWorkflowRunner`` at all — every reachable service becomes
            available, matching this function's original behavior); pass your own callable
            for a custom policy.
        **kwargs: Forwarded to ``agents.mcp.MCPServer.__init__`` — ``require_approval``,
            ``failure_error_function``, ``tool_meta_resolver``, ``custom_data_extractor``,
            ``use_structured_content``. (Not ``cache_tools_list``/``tool_filter``/
            ``max_retry_attempts``/``client_session_timeout_seconds`` — those belong to
            ``_MCPServerWithClientSession``, a real out-of-process-session strategy this one
            doesn't use; see ``_nexus_mcp.py``'s module docstring for why.)

    Example (manual usage — prefer ``OpenAIAgentsPlugin(nexus_transport=True)``)::

        from temporalio import workflow
        from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
            nexus_transport_mcp_server,
        )
        from agents import Agent, Runner

        @workflow.defn(sandboxed=False)
        class NexusToolsAgent:
            @workflow.run
            async def run(self, query: str) -> str:
                async with nexus_transport_mcp_server() as mcp_server:
                    agent = Agent(
                        name="Assistant",
                        instructions="Use the available tools to help with the request.",
                        mcp_servers=[mcp_server],
                    )
                    result = await Runner.run(agent, input=query)
                    return result.final_output
    """
    from temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp import (
        _NexusTransportMCPServer,
    )

    def _default_enabled_services() -> frozenset[str]:
        from temporal_agent_harness.harness.agent_workflow import (
            current_agent_workflow_runner,
        )

        runner = current_agent_workflow_runner()
        return runner.enabled_mcp_servers if runner is not None else frozenset()

    resolved_enabled_services = (
        _default_enabled_services
        if enabled_services is _ENABLED_SERVICES_UNSET
        else enabled_services
    )

    return _NexusTransportMCPServer(
        nexus_mcp_server_registry().servers,
        name=name,
        enabled_services=resolved_enabled_services,
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
