"""Activity wrappers for executing LangGraph nodes and tasks."""

import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from inspect import iscoroutinefunction, signature
from typing import Any, Callable

from langgraph.errors import GraphInterrupt
from langgraph.types import Command, Interrupt, Send, TimeoutPolicy

from temporalio import workflow
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporal_agent_harness.ai_sdks.langgraph._langgraph_config import (
    context_schema_id,
    get_langgraph_config,
    is_context_schema,
    set_langgraph_config,
    strip_runnable_config,
)
from temporal_agent_harness.ai_sdks.langgraph._task_cache import (
    cache_key,
    cache_lookup,
    cache_put,
)

# Per-run dedupe so we only warn once when a user passes a Store via
# graph.compile(store=...) / @entrypoint(store=...). Cleared by
# LangGraphInterceptor.execute_workflow on workflow exit.
_warned_store_runs: set[str] = set()


def clear_store_warning(run_id: str) -> None:
    """Drop the store-warning dedupe entry for a workflow run."""
    _warned_store_runs.discard(run_id)


@dataclass
class ActivityInput:
    """Input for a LangGraph activity, containing args, kwargs, and config."""

    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    langgraph_config: dict[str, Any]


@dataclass
class ActivityOutput:
    """Output from an Activity, containing result, command, or interrupts."""

    result: Any = None
    langgraph_command: Any = None
    langgraph_interrupts: tuple[Interrupt] | None = None


def _command_to_payload(command: Command) -> dict[str, Any]:
    """Convert a ``Command`` to a dict the data converter can serialize.

    ``Send`` objects in ``goto`` become ``{"node", "arg", "timeout"}`` dicts.
    """

    def send_to_dict(item: Any) -> Any:
        if isinstance(item, Send):
            return {"node": item.node, "arg": item.arg, "timeout": item.timeout}
        return item

    goto = command.goto
    if isinstance(goto, Sequence) and not isinstance(goto, str):
        goto = [send_to_dict(item) for item in goto]
    else:
        goto = send_to_dict(goto)
    return {
        "graph": command.graph,
        "update": command.update,
        "resume": command.resume,
        "goto": goto,
    }


def _command_from_payload(payload: dict[str, Any]) -> Command:
    """Rebuild a ``Command`` from ``_command_to_payload`` output.

    Node names are the only other values ``goto`` can hold, so a dict in
    ``goto`` is always a serialized ``Send``. Payloads written before ``Send``
    support only hold node names and pass through unchanged.
    """

    def dict_to_send(item: Any) -> Any:
        if isinstance(item, dict):
            timeout = item.get("timeout")
            return Send(
                item["node"],
                item["arg"],
                timeout=TimeoutPolicy(**timeout) if timeout else None,
            )
        return item

    goto = payload["goto"]
    if isinstance(goto, list):
        goto = [dict_to_send(item) for item in goto]
    else:
        goto = dict_to_send(goto)
    return Command(
        graph=payload["graph"],
        update=payload["update"],
        resume=payload["resume"],
        goto=goto,
    )


def wrap_activity(
    func: Callable,
    *,
    context_schema: type[Any] | None = None,
    context_schemas: dict[str, type[Any]] | None = None,
    streaming_topic: str | None = None,
    streaming_batch_interval: timedelta = timedelta(milliseconds=100),
) -> Callable[[ActivityInput], Awaitable[ActivityOutput]]:
    """Wrap a function as a Temporal activity that handles LangGraph config and interrupts."""
    accepts_runtime = "runtime" in signature(func).parameters

    async def wrapper(input: ActivityInput) -> ActivityOutput:
        async def run(stream_writer: Callable[[Any], None] | None) -> ActivityOutput:
            # Sync funcs run on a thread (so the loop keeps flushing the
            # stream client mid-execution); marshal writer calls back to
            # the loop thread because the client's flush event is an
            # asyncio.Event and isn't safe to set off-thread.
            effective_writer = stream_writer
            if not iscoroutinefunction(func) and stream_writer is not None:
                loop = asyncio.get_running_loop()
                inner_writer = stream_writer

                def thread_safe_writer(value: Any) -> None:
                    loop.call_soon_threadsafe(inner_writer, value)

                effective_writer = thread_safe_writer

            schema = context_schema
            schema_id = input.langgraph_config.get("context_schema_id")
            if schema_id is not None:
                schema = (context_schemas or {}).get(schema_id)
                if schema is None:
                    raise ValueError(f"Unknown context schema ID: {schema_id!r}")
            runtime = set_langgraph_config(
                input.langgraph_config,
                context_schema=schema,
                stream_writer=effective_writer,
            )
            kwargs = dict(input.kwargs)
            if accepts_runtime:
                kwargs["runtime"] = runtime

            try:
                if iscoroutinefunction(func):
                    result = await func(*input.args, **kwargs)
                else:
                    result = await asyncio.to_thread(func, *input.args, **kwargs)
                if isinstance(result, Command):
                    return ActivityOutput(langgraph_command=_command_to_payload(result))
                return ActivityOutput(result=result)
            except GraphInterrupt as e:
                return ActivityOutput(langgraph_interrupts=e.args[0])

        if streaming_topic is None:
            return await run(stream_writer=None)
        async with WorkflowStreamClient.from_within_activity(
            batch_interval=streaming_batch_interval,
        ) as client:
            topic = client.topic(streaming_topic)
            return await run(stream_writer=topic.publish)

    return wrapper


def wrap_execute_activity(
    afunc: Callable[[ActivityInput], Awaitable[ActivityOutput]],
    task_id: str = "",
    summary_fn: Callable[[tuple[Any, ...], dict[str, Any]], str | None] | None = None,
    context_schemas: dict[str, type[Any]] | None = None,
    **execute_activity_kwargs: Any,
) -> Callable[..., Any]:
    """Wrap an activity function to be called via workflow.execute_activity with caching."""

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        # LangGraph may inject a RunnableConfig as the 'config' kwarg. Strip it
        # down to a serializable subset so it can cross the activity boundary;
        # callbacks, stores, etc. aren't serializable.
        if "config" in kwargs:
            kwargs["config"] = strip_runnable_config(kwargs["config"])

        # LangGraph may inject a Runtime as the 'runtime' kwarg. It's
        # reconstructed on the activity side from the serialized langgraph
        # config, so drop the live Runtime from the kwargs that cross the
        # activity boundary (it holds non-serializable stream_writer, store).
        runtime = kwargs.pop("runtime", None)
        run_id = workflow.info().run_id
        if (
            getattr(runtime, "store", None) is not None
            and run_id not in _warned_store_runs
        ):
            _warned_store_runs.add(run_id)
            workflow.logger.warning(
                "LangGraph Store passed via compile(store=...) / @entrypoint(store=...) "
                "is not accessible inside activity-wrapped nodes and tasks: the Store "
                "object isn't serializable across the activity boundary, and activities "
                "may run on a different worker than the workflow. Use a backend-backed "
                "store (Postgres/Redis) configured on each worker if you need shared "
                "memory, or use workflow state for per-run memory."
            )

        langgraph_config = get_langgraph_config()
        if runtime is not None:
            langgraph_config["context"] = runtime.context
        context_type = type(langgraph_config.get("context"))
        if is_context_schema(context_type):
            schema_id = context_schema_id(context_type)
            if schema_id not in (context_schemas or {}):
                raise ValueError(
                    f"Context type {schema_id!r} is not registered. "
                    "Declare it as a graph or entrypoint context_schema."
                )
            langgraph_config["context_schema_id"] = schema_id

        # Check task result cache (for continue-as-new deduplication).
        key = (
            cache_key(task_id, args, kwargs, langgraph_config.get("context"))
            if task_id
            else ""
        )
        # The cache travels through workflow.continue_as_new(), so a Command is
        # stored in its payload form under a separate "<key>:command" entry and
        # rebuilt on lookup. cache_key() returns 32 hex characters, so this
        # namespace can't collide with a plain result key, and a result dict
        # is never mistaken for a Command.
        if task_id:
            found, cached = cache_lookup(f"{key}:command")
            if found:
                return _command_from_payload(cached)
            found, cached = cache_lookup(key)
            if found:
                return cached

        input = ActivityInput(
            args=args, kwargs=kwargs, langgraph_config=langgraph_config
        )
        # Compute a dynamic activity summary (if configured) on the schedule
        # path only; a cache hit above returns before reaching here, so no
        # activity is scheduled and no summary is needed.
        call_kwargs = dict(execute_activity_kwargs)
        if summary_fn is not None:
            summary = summary_fn(args, kwargs)
            if summary:
                call_kwargs["summary"] = summary
        output = await workflow.execute_activity(afunc, input, **call_kwargs)
        if output.langgraph_interrupts is not None:
            raise GraphInterrupt(output.langgraph_interrupts)

        result = output.result
        cached = result
        if output.langgraph_command is not None:
            result = _command_from_payload(output.langgraph_command)
            cached = output.langgraph_command

        # Store in cache for future continue-as-new cycles.
        if task_id:
            cache_put(
                f"{key}:command" if output.langgraph_command is not None else key,
                cached,
            )

        return result

    return wrapper
