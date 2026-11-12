"""LangGraph plugin for running LangGraph nodes and tasks as Temporal activities."""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import importlib.metadata
import inspect
import sys
import warnings
from dataclasses import replace
from datetime import timedelta
from functools import partial, update_wrapper
from typing import Any, Callable

from langgraph._internal._runnable import RunnableCallable
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.pregel import Pregel

from temporalio import activity, workflow
from temporal_agent_harness.ai_sdks.integration_helpers import ObserverFactory
from temporal_agent_harness.ai_sdks.langgraph._activity import (
    STREAM_ROUTE_KEY,
    ValueTypes,
    wrap_activity,
    wrap_execute_activity,
)
from temporal_agent_harness.ai_sdks.langgraph._interceptor import (
    LangGraphInterceptor,
    _workflow_entrypoints,
    _workflow_graphs,
)
from temporal_agent_harness.ai_sdks.langgraph._langgraph_config import context_schema_id, is_context_schema
from temporal_agent_harness.ai_sdks.langgraph._task_cache import (
    get_task_cache,
    set_task_cache,
    task_id,
)
from temporal_agent_harness.ai_sdks.langgraph._workflow import wrap_workflow
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

_ACTIVITY_OPTION_KEYS: frozenset[str] = frozenset(
    {"execute_in", *inspect.signature(workflow.execute_activity).parameters}
)
# Node/task option keys beyond the raw execute_activity parameters:
# 'summary_fn' is a callable consumed in the workflow (not a Temporal
# option), so it must be split out of Graph API metadata too.
_LANGGRAPH_OPTION_KEYS: frozenset[str] = _ACTIVITY_OPTION_KEYS | frozenset(
    {"summary_fn"}
)


def _constant_summary_fn(
    value: str,
) -> Callable[[tuple[Any, ...], dict[str, Any]], str]:
    """Adapt a static summary string to the summary_fn interface."""
    return lambda args, kwargs: value


def _task_cache_id(func: Callable, activity_name: str) -> str:
    """Return the continue-as-new cache id for an activity-bound node or task.

    Node factories return closures, and ``task_id`` rejects those: every
    closure from one factory shares a qualname, so it cannot identify a
    node. A Graph API node's Temporal identity is its ``<graph>.<node>``
    activity name regardless of its function, so that name — prefixed to
    keep it out of the ``module.qualname`` id space — identifies the
    node's cached results just as well. Only closures and local functions
    fall back; every other ``task_id`` rejection (``__main__`` functions,
    callables without a qualname) still propagates. Functions with a
    stable ``module.qualname`` keep it, so caches written by earlier
    versions (and the histories carrying them) stay valid.

    Args:
        func: Activity-bound node or task function.
        activity_name: Activity name whose results are cached when ``func``
            is a closure or local function.

    Returns:
        The id the continue-as-new result cache keys ``func``'s entries by.
    """
    try:
        return task_id(func)
    except ValueError:
        qualname = (
            getattr(func, "__qualname__", None) or getattr(func, "__name__", None) or ""
        )
        if "<locals>" not in qualname:
            raise
        return f"graph-node:{activity_name}"


def _merge_activity_opts(
    defaults: dict[str, Any] | None, specific: dict[str, Any]
) -> dict[str, Any]:
    """Layer per-node/task options over the plugin defaults.

    ``summary`` and ``summary_fn`` are two forms of one setting, so a node or
    task that supplies either form overrides an inherited default of *either*
    form, rather than coexisting with it and tripping the exclusivity check.
    """
    merged = dict(defaults or {})
    if "summary" in specific or "summary_fn" in specific:
        merged.pop("summary", None)
        merged.pop("summary_fn", None)
    merged.update(specific)
    return merged


class LangGraphPlugin(SimplePlugin):
    """LangGraph plugin for Temporal SDK.

    .. warning::
        This package is experimental and may change in future versions.
        Use with caution in production environments.

    This plugin runs `LangGraph <https://github.com/langchain-ai/langgraph>`_ nodes
    and tasks as Temporal Activities, giving your AI agent workflows durable
    execution, automatic retries, and timeouts. It supports both the LangGraph Graph
    API (``StateGraph``) and Functional API (``@entrypoint`` / ``@task``).

    Args:
        graphs: Graph API graphs to make available to workflows, keyed by name.
            Workflows retrieve them with :func:`graph` and call
            ``.compile()`` to get a runnable. Each node's ``metadata`` must
            include ``execute_in`` (``"activity"`` or ``"workflow"``) and
            may include any kwarg accepted by
            :func:`workflow.execute_activity` (e.g. ``start_to_close_timeout``,
            ``retry_policy``).
        entrypoints: Functional API entrypoints to make available to
            workflows, keyed by name. Workflows retrieve them with
            :func:`entrypoint`.
        tasks: Functional API ``@task`` functions to wrap as Temporal
            Activities.
        activity_options: Per-task activity options for the Functional
            API, keyed by task function name. Each entry must include
            ``execute_in`` and may include any
            :func:`workflow.execute_activity` kwarg. Used because LangGraph's
            Functional API has no per-task ``metadata`` channel.
        default_activity_options: Activity options applied to every
            activity-bound node and task, overridable per-node (Graph API
            ``metadata``) or per-task (``activity_options[name]``). A
            node/task that sets ``summary`` or ``summary_fn`` overrides an
            inherited default of either form.
        streaming_topic: When set, ``langgraph.config.get_stream_writer()``
            inside a node publishes to this topic on the workflow's
            :class:`WorkflowStream`. The workflow must construct
            ``WorkflowStream()`` in its ``@workflow.init`` (the plugin's
            interceptor verifies this on workflow start). Nodes with
            ``execute_in='activity'`` publish through
            :class:`WorkflowStreamClient` (signal); nodes with
            ``execute_in='workflow'`` publish synchronously to the
            in-workflow stream (no signal).
        streaming_batch_interval: How often the activity-side stream
            client flushes buffered publishes into a single
            ``__temporal_workflow_stream_publish`` signal. Has no effect
            on workflow-side nodes (their publishes are synchronous
            in-memory log appends). Lower values reduce streaming
            latency at the cost of more signals (more workflow history
            events); higher values amortize signal cost but make
            chunks arrive in larger bursts. Default 100ms suits
            interactive token streaming; raise to 250–1000ms for
            non-interactive aggregation, lower toward 10–50ms only if
            you've measured the latency need and accept the history
            cost. Also the flush cadence handed to ``observer_factory``.
        awrap_tool_call: Harness: an ``awrap_tool_call`` for every Graph API
            ``ToolNode`` that doesn't set its own, as ``ToolNode(...,
            awrap_tool_call=...)`` would.
        observer_factory: Harness: builds the live observer for activity
            calls whose run has a stream route (``graph(name,
            stream_route=...)``). The observer receives the call's LangChain
            callback events as raw events.
    """

    def __init__(
        self,
        # Graph API
        graphs: dict[str, StateGraph[Any, Any, Any, Any]] | None = None,
        # Functional API
        entrypoints: dict[str, Pregel[Any, Any, Any, Any]] | None = None,
        tasks: list | None = None,
        # TODO: Remove activity_options when we have support for @task(metadata=...)
        activity_options: dict[str, dict[str, Any]] | None = None,
        default_activity_options: dict[str, Any] | None = None,
        streaming_topic: str | None = None,
        streaming_batch_interval: timedelta = timedelta(milliseconds=100),
        # Harness: a tool-call wrapper for every ToolNode, and the live observer factory.
        awrap_tool_call: Callable[..., Any] | None = None,
        observer_factory: ObserverFactory | None = None,
    ):
        """Initialize the LangGraph plugin with graphs, entrypoints, and tasks.

        .. warning::
            Streaming support is experimental and may change in
            future versions.
        """
        if sys.version_info < (3, 11):
            warnings.warn(  # type: ignore[reportUnreachable]
                "LangGraphPlugin requires Python >= 3.11 for full async support. "
                "On older versions, the Functional API (@task/@entrypoint) and "
                "interrupt() will not work because LangGraph relies on "
                "contextvars propagation through asyncio.create_task(), which is "
                "only available in Python 3.11+. See "
                "https://reference.langchain.com/python/langgraph/config/get_store/",
                stacklevel=2,
            )

        if default_activity_options and "execute_in" in default_activity_options:
            raise ValueError(
                "execute_in cannot be set in default_activity_options. "
                "Set it on each node's metadata (Graph API) or in "
                "activity_options[task_name] (Functional API)."
            )

        if (
            default_activity_options
            and "summary" in default_activity_options
            and "summary_fn" in default_activity_options
        ):
            raise ValueError(
                "Set either 'summary' or 'summary_fn' in default_activity_options, "
                "not both."
            )

        self.activities: list = []
        self._streaming_topic = streaming_topic
        self._streaming_batch_interval = streaming_batch_interval
        self._observer_factory = observer_factory
        self._context_schemas: dict[str, type[Any]] = {}
        schemas: list[type[Any] | None] = [
            *(graph.context_schema for graph in (graphs or {}).values()),
            *(program.context_schema for program in (entrypoints or {}).values()),
        ]
        for schema in schemas:
            if schema is not None and is_context_schema(schema):
                schema_id = context_schema_id(schema)
                previous = self._context_schemas.get(schema_id)
                if previous is not None and previous is not schema:
                    raise ValueError(f"Conflicting context schema ID: {schema_id!r}")
                self._context_schemas[schema_id] = schema
        self._value_types = ValueTypes(
            schemas=[
                schema for graph in (graphs or {}).values() for schema in graph.schemas
            ],
            functions=[
                *(
                    getattr(node.runnable, "func", None)
                    or getattr(node.runnable, "afunc", None)
                    for graph in (graphs or {}).values()
                    for node in graph.nodes.values()
                ),
                *(task.func for task in tasks or []),
            ],
        )

        # Graph API: Wrap graph nodes as Temporal Activities.
        if graphs:
            for graph_name, graph in graphs.items():
                for node_name, node in graph.nodes.items():
                    if node.retry_policy:
                        raise ValueError(
                            f"Node {graph_name}.{node_name} has a LangGraph "
                            f"retry_policy set. Use Temporal activity options "
                            f"instead, e.g. pass retry_policy=RetryPolicy(...) "
                            f"via default_activity_options or in the node's "
                            f"metadata dict."
                        )
                    runnable = node.runnable
                    if not isinstance(runnable, RunnableCallable):
                        raise ValueError(f"Node {node_name} must be a RunnableCallable")
                    # Harness: a ToolNode without its own tool-call wrapper gets the plugin's.
                    if (
                        awrap_tool_call is not None
                        and isinstance(runnable, ToolNode)
                        and runnable._awrap_tool_call is None
                    ):
                        runnable._awrap_tool_call = awrap_tool_call
                    # Harness: only ToolNode's async path calls awrap_tool_call, so a
                    # ToolNode with one runs through afunc. Keep the sync path otherwise:
                    # sync tools need run_in_executor, which the workflow event loop
                    # doesn't provide. Not upstream yet.
                    user_func = (
                        runnable.afunc
                        if isinstance(runnable, ToolNode)
                        and runnable._awrap_tool_call is not None
                        else runnable.func or runnable.afunc
                    )
                    if user_func is None:
                        raise ValueError(f"Node {node_name} must have a function")
                    if getattr(user_func, "__self__", None) is runnable:
                        # Harness: compile() follows a method bound to the node back to
                        # it, which never returns once afunc is replaced below. Not
                        # upstream yet.
                        user_func = update_wrapper(
                            partial(getattr(user_func, "__func__"), runnable),
                            user_func,
                        )
                    # Keep 'config' (for metadata/tags) and 'runtime' (for
                    # context + store — reconstructed on the activity side).
                    # Drop writer/etc., which hold non-serializable objects
                    # that can't cross the activity boundary.
                    runnable.func_accepts = {
                        k: v
                        for k, v in runnable.func_accepts.items()
                        if k in ("config", "runtime")
                    }
                    # Split node.metadata into activity options vs. user
                    # metadata. Activity-option keys (timeouts, retry policy,
                    # etc.) become kwargs to workflow.execute_activity; user
                    # keys stay on node.metadata so LangGraph exposes them to
                    # the node function via config["metadata"].
                    node_meta = node.metadata or {}
                    node_opts = {
                        k: v
                        for k, v in node_meta.items()
                        if k in _LANGGRAPH_OPTION_KEYS
                    }
                    node.metadata = {
                        k: v
                        for k, v in node_meta.items()
                        if k not in _LANGGRAPH_OPTION_KEYS
                    }
                    if "execute_in" not in node_opts:
                        raise ValueError(
                            f"Node {graph_name}.{node_name} is missing required "
                            f"'execute_in' in metadata. Set it to 'activity' or "
                            f"'workflow'."
                        )
                    opts = _merge_activity_opts(default_activity_options, node_opts)
                    if opts["execute_in"] == "activity":
                        # Capture context even when the user calls get_runtime()
                        # without declaring a runtime parameter.
                        runnable.func_accepts["runtime"] = (
                            "N/A",
                            inspect.Parameter.empty,
                        )
                    # Route all LangGraph node calls through afunc so the async
                    # activity wrapper is always used. wrap_activity handles
                    # sync vs. async user functions inside the activity itself.
                    runnable.afunc = self.execute(
                        f"{graph_name}.{node_name}",
                        user_func,
                        opts,
                        context_schema=graph.context_schema,
                    )
                    runnable.func = None

        # Functional API: Wrap @task functions as Temporal Activities.
        if tasks:
            for task in tasks:
                name = task.func.__name__
                if task.retry_policy:
                    raise ValueError(
                        f"Task {name} has a LangGraph retry_policy set. "
                        f"Use Temporal activity options instead, e.g. pass "
                        f"retry_policy=RetryPolicy(...) via "
                        f"default_activity_options or activity_options[{name!r}]."
                    )
                task_opts = (activity_options or {}).get(name, {})
                if "execute_in" not in task_opts:
                    raise ValueError(
                        f"Task {name} is missing required 'execute_in' in "
                        f"activity_options[{name!r}]. Set it to 'activity' or "
                        f"'workflow'."
                    )
                opts = _merge_activity_opts(default_activity_options, task_opts)

                task.func = self.execute(task_id(task.func), task.func, opts)
                task.func.__name__ = name
                task.func.__qualname__ = getattr(task.func, "__qualname__", name)

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if not runner:
                raise ValueError("No WorkflowRunner provided to the LangGraph plugin.")
            if isinstance(runner, SandboxedWorkflowRunner):
                return replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        # Harness: upstream lives under ``temporalio``, which the sandbox
                        # passes through by default; this copy does not, so pass it
                        # through explicitly.
                        "temporal_agent_harness.ai_sdks.langgraph",
                        "langchain",
                        "langchain_core",
                        "langgraph",
                        "langsmith",
                        "numpy",  # LangSmith uses numpy
                    ),
                )
            return runner

        super().__init__(
            "langchain.LangGraphPlugin",
            activities=self.activities,
            workflow_runner=workflow_runner,
            interceptors=[
                LangGraphInterceptor(
                    graphs or {}, entrypoints or {}, streaming_topic=streaming_topic
                )
            ],
        )

    def version(self) -> str:
        """Return this package's version, which worker heartbeats report."""
        try:
            # Harness: this copy ships inside the harness distribution.
            return importlib.metadata.version("temporal-agent-harness")
        except importlib.metadata.PackageNotFoundError:
            return ""

    def execute(
        self,
        activity_name: str,
        func: Callable,
        kwargs: dict[str, Any] | None = None,
        *,
        context_schema: type[Any] | None = None,
    ) -> Callable:
        """Prepare a node or task to execute as an activity or inline in the workflow."""
        opts = kwargs or {}
        execute_in = opts.pop("execute_in")
        # Normalize the node's summary to a single summary_fn. Both keys are
        # popped so neither reaches workflow.execute_activity (which takes no
        # summary_fn, and would get a duplicate summary kwarg); a static
        # summary becomes a summary_fn that ignores its input.
        summary_fn = opts.pop("summary_fn", None)
        static_summary = opts.pop("summary", None)
        if summary_fn is not None and static_summary is not None:
            raise ValueError(
                f"{activity_name}: set either 'summary' or 'summary_fn', not both."
            )
        if static_summary is not None:
            summary_fn = _constant_summary_fn(static_summary)

        if execute_in == "activity":
            wrapped = wrap_activity(
                func,
                context_schema=context_schema,
                context_schemas=self._context_schemas,
                value_types=self._value_types,
                streaming_topic=self._streaming_topic,
                streaming_batch_interval=self._streaming_batch_interval,
                observer_factory=self._observer_factory,
            )
            a = activity.defn(name=activity_name)(wrapped)
            self.activities.append(a)
            return wrap_execute_activity(
                a,
                task_id=_task_cache_id(func, activity_name),
                summary_fn=summary_fn,
                context_schemas=self._context_schemas,
                value_types=self._value_types,
                **opts,
            )
        elif execute_in == "workflow":
            return wrap_workflow(
                func, streaming_topic=self._streaming_topic, summary_fn=summary_fn
            )
        else:
            raise ValueError(f"Invalid execute_in value: {execute_in}")


def graph(
    name: str,
    cache: dict[str, Any] | None = None,
    *,
    # Harness: see ``stream_route`` below.
    stream_route: Callable[[], Any] | None = None,
) -> StateGraph[Any, Any, Any, Any] | _RoutedGraph:
    """Retrieve a registered graph by name.

    Args:
        name: Graph name as registered with LangGraphPlugin.
        cache: Optional task result cache from a previous cache() call.
            Restores cached results so previously-completed nodes are
            not re-executed after continue-as-new.
        stream_route: Harness: called in workflow code before each activity
            call of the compiled graph; a routing token it returns is handed to
            the plugin's ``observer_factory`` for that call, and ``None`` means
            the call doesn't stream. Given, ``graph()`` returns the graph to
            ``compile()`` as usual, with the route bound to its runs.
    """
    set_task_cache(cache or {})
    graphs = _workflow_graphs.get(workflow.info().run_id)
    if graphs is None:
        raise RuntimeError(
            "graph() must be called from inside a workflow running under LangGraphPlugin"
        )
    if name not in graphs:
        raise KeyError(f"Graph {name!r} not found. Available graphs: {list(graphs)}")
    if stream_route is None:
        return graphs[name]
    return _RoutedGraph(graphs[name], stream_route)


class _RoutedGraph:
    """Harness: a registered graph whose compiled runs carry a stream route."""

    def __init__(
        self, builder: StateGraph[Any, Any, Any, Any], stream_route: Callable[[], Any]
    ) -> None:
        self.builder = builder
        self._stream_route = stream_route

    def compile(self, **kwargs: Any) -> Pregel[Any, Any, Any, Any]:
        return self.builder.compile(**kwargs).with_config(
            {"configurable": {STREAM_ROUTE_KEY: self._stream_route}}
        )


def entrypoint(
    name: str,
    cache: dict[str, Any] | None = None,
    *,
    # Harness: as ``graph()``'s ``stream_route``.
    stream_route: Callable[[], Any] | None = None,
) -> Pregel[Any, Any, Any, Any]:
    """Retrieve a registered entrypoint by name.

    Args:
        name: Entrypoint name as registered with Plugin.
        cache: Optional task result cache from a previous cache() call.
            Restores cached results so previously-completed tasks are
            not re-executed after continue-as-new.
        stream_route: Harness: as :func:`graph`'s, bound to the entrypoint's runs.
    """
    set_task_cache(cache or {})
    entrypoints = _workflow_entrypoints.get(workflow.info().run_id)
    if entrypoints is None:
        raise RuntimeError(
            "entrypoint() must be called from inside a workflow running under LangGraphPlugin"
        )
    if name not in entrypoints:
        raise KeyError(
            f"Entrypoint {name!r} not found. Available entrypoints: {list(entrypoints)}"
        )
    if stream_route is None:
        return entrypoints[name]
    return entrypoints[name].with_config({"configurable": {STREAM_ROUTE_KEY: stream_route}})


def cache() -> dict[str, Any] | None:
    """Return the task result cache as a serializable dict.

    Returns a dict suitable for passing to entrypoint(name, cache=...) to
    restore cached task results across continue-as-new boundaries.
    Returns None if the cache is empty.
    """
    return get_task_cache() or None
