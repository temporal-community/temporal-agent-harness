"""Activity wrappers for executing LangGraph nodes and tasks."""

import asyncio
import dataclasses
import sys
import warnings
from collections.abc import AsyncIterator, Awaitable, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from inspect import get_annotations, iscoroutinefunction, signature
from typing import (
    Annotated,
    Any,
    Callable,
    Literal,
    get_args,
    get_origin,
    get_type_hints,
)

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, LLMResult
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, Interrupt, Send, TimeoutPolicy
from pydantic import BaseModel
from typing_extensions import is_typeddict

from temporalio import activity, workflow
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.converter import PayloadConverter
from temporalio.exceptions import ApplicationError
from temporal_agent_harness.ai_sdks.integration_helpers import ObserverFactory, select_observer
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


# Harness: the run-config key that carries a graph's stream route (see
# ``graph(stream_route=...)``): a callable that, in workflow code, returns the routing
# token for each activity call, or ``None`` for a call that should not stream.
STREAM_ROUTE_KEY = "__temporal_langgraph_stream_route"


@dataclass
class ActivityInput:
    """Input for a LangGraph activity, containing args, kwargs, and config."""

    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    langgraph_config: dict[str, Any]
    value_types: list[Any] | None = None
    # Harness: an opaque routing token for the plugin's observer factory. Typed ``Any``
    # because only the factory knows its shape; it arrives as plain JSON.
    stream_to: Any = None


@dataclass
class ActivityOutput:
    """Output from an Activity, containing result, command, or interrupts."""

    result: Any = None
    langgraph_command: Any = None
    langgraph_interrupts: tuple[Interrupt] | None = None
    value_types: list[Any] | None = None


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
            if isinstance(timeout, dict):  # Not one rebuilt as a known class
                timeout = TimeoutPolicy(**timeout) if timeout else None
            return Send(item["node"], item["arg"], timeout=timeout)
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


_COMMAND_TYPE_ID = "langgraph.types.Command"
# Marks the type ID of a node in the tree ValueTypes.restore builds from paths.
_TYPE_ID = object()
_MISSING = object()


def _subclasses(cls: type) -> Iterator[type]:
    for subclass in cls.__subclasses__():
        yield subclass
        yield from _subclasses(subclass)


def _type_hints(obj: Any) -> list[Any]:
    """Return the type hints of a function or class that can be resolved."""
    try:
        return list(get_type_hints(obj, include_extras=True).values())
    except Exception:
        pass
    # Resolve the hints one at a time, so one that can't be resolved, such as
    # a name imported only for type checking, doesn't hide the others.
    hints: list[Any] = []
    for owner in reversed(obj.__mro__) if isinstance(obj, type) else [obj]:
        module = sys.modules.get(getattr(owner, "__module__", None) or "")
        globalns = getattr(owner, "__globals__", vars(module) if module else {})
        try:
            annotations = get_annotations(owner)
        except Exception:
            continue
        for hint in annotations.values():
            try:
                hints.append(eval(hint, globalns) if isinstance(hint, str) else hint)
            except Exception:
                pass
    return hints


def _importable(cls: type) -> bool:
    """Whether ``cls`` is the class its module holds under its name."""
    owner: Any = sys.modules.get(cls.__module__)
    for name in cls.__qualname__.split("."):
        owner = getattr(owner, name, None)
    return owner is cls


def _convert(converter: PayloadConverter, value: Any, hint: Any) -> Any:
    """Rebuild plain JSON ``value`` as ``hint`` with ``converter``, or ``_MISSING``."""
    try:
        return converter.from_payload(converter.to_payload(value), hint)
    except Exception:
        return _MISSING


class ValueTypes:
    """Keep the types of node and task values across the Activity boundary.

    Activity inputs and outputs hold these values as ``Any``, so the payload
    converter decodes them as plain JSON. The sender records where values of
    known classes sit in dicts, lists and tuples, by class ID, and the receiver
    rebuilds them with the payload converter, as for an Activity parameter of
    that class. Values inside a rebuilt value are what the converter makes of
    them. Values are encoded as before, so older payloads and readers are
    unaffected.

    Known classes are ``bytes``, LangChain messages, and the Pydantic models
    and dataclasses reachable from the given schemas and function signatures,
    with their subclasses. Only these are rebuilt.
    """

    def __init__(
        self, schemas: Iterable[Any] = (), functions: Iterable[Any] = ()
    ) -> None:
        """Collect the classes reachable from ``schemas`` and ``functions``."""
        found: dict[str, list[type]] = {}
        # Keyed by id() because some hints are unhashable; keeps them alive.
        seen: dict[int, Any] = {}
        stack = [bytes, *_subclasses(BaseMessage), *schemas]
        for function in functions:
            stack.extend(_type_hints(function))
        while stack:
            hint = stack.pop()
            if id(hint) in seen:
                continue
            seen[id(hint)] = hint
            origin = get_origin(hint)
            if origin is Literal:
                continue
            if origin is not None:
                args = get_args(hint)
                stack.extend(args[:1] if origin is Annotated else args)
                continue
            if not isinstance(hint, type):
                continue
            if issubclass(hint, BaseModel):
                stack.extend(field.annotation for field in hint.model_fields.values())
                if hint is not BaseModel:
                    stack.extend(_subclasses(hint))
            elif dataclasses.is_dataclass(hint):
                stack.extend(_type_hints(hint))
                stack.extend(_subclasses(hint))
            elif hint is not bytes:
                if is_typeddict(hint):
                    stack.extend(_type_hints(hint))
                continue
            found.setdefault(context_schema_id(hint), []).append(hint)
        classes: dict[str, type] = {}
        for type_id, candidates in found.items():
            # The sandbox imports Workflow modules again, so a subclass of a
            # passed-through class, such as a message class, can appear twice.
            if len(candidates) > 1:
                candidates = [cls for cls in candidates if _importable(cls)]
            if len(candidates) == 1:
                classes[type_id] = candidates[0]
            else:
                warnings.warn(
                    f"Classes with the same ID {type_id!r} are not rebuilt "
                    "after crossing an Activity boundary.",
                    stacklevel=2,
                )
        self._classes = classes
        self._ids = {cls: type_id for type_id, cls in classes.items()}

    def _type_id(self, cls: type) -> str | None:
        type_id = self._ids.get(cls)
        # Workflow code can build values from the sandbox's copy of a class.
        if type_id is None and (
            issubclass(cls, BaseModel) or dataclasses.is_dataclass(cls)
        ):
            candidate = context_schema_id(cls)
            if candidate in self._classes:
                type_id = candidate
        return type_id

    def encode(self, value: Any, path: list[str | int], value_types: list[Any]) -> Any:
        """Record known values in ``value`` and put its Commands in payload form."""
        if isinstance(value, Command):
            payload = self.encode(_command_to_payload(value), path, value_types)
            value_types.append([path, _COMMAND_TYPE_ID])
            return payload
        type_id = self._type_id(type(value))
        if type_id is not None:
            value_types.append([path, type_id])
            return value
        if isinstance(value, dict):
            items = {
                key: self.encode(item, [*path, key], value_types)
                if isinstance(key, (str, int)) and not isinstance(key, bool)
                else item
                for key, item in value.items()
            }
            changed = any(items[key] is not value[key] for key in value)
            return items if changed else value
        if isinstance(value, (list, tuple)):
            elements = [
                self.encode(item, [*path, index], value_types)
                for index, item in enumerate(value)
            ]
            changed = any(new is not old for new, old in zip(elements, value))
            return elements if changed else value
        return value

    def restore(
        self, value: Any, value_types: list[Any], converter: PayloadConverter
    ) -> Any:
        """Rebuild the values ``encode`` recorded, leaving ``value`` unchanged."""
        tree: dict[Any, Any] = {}
        for path, type_id in value_types:
            node = tree
            for key in path:
                node = node.setdefault(key, {})
            node[_TYPE_ID] = type_id

        def rebuild(value: Any, node: dict[Any, Any]) -> Any:
            children = [key for key in node if key is not _TYPE_ID]
            if children:
                value = dict(value) if isinstance(value, dict) else list(value)
                for key in children:
                    # JSON writes an int key as a string.
                    found = (
                        str(key)
                        if isinstance(value, dict) and key not in value
                        else key
                    )
                    value[found] = rebuild(value[found], node[key])
            if _TYPE_ID in node:
                value = self._rebuild(node[_TYPE_ID], value, converter)
            return value

        return rebuild(value, tree)

    def _rebuild(self, type_id: str, value: Any, converter: PayloadConverter) -> Any:
        if type_id == _COMMAND_TYPE_ID:
            return _command_from_payload(value)
        cls = self._classes.get(type_id)
        if cls is None:
            raise ApplicationError(
                f"Unknown value type ID {type_id!r}: this Worker didn't find its "
                "class. All Workers must register the same graphs and tasks, "
                "and import the same message classes.",
                non_retryable=True,
            )
        non_init: list[str] = []
        if dataclasses.is_dataclass(cls) and isinstance(value, dict):
            non_init = [f.name for f in dataclasses.fields(cls) if not f.init]
        # The default converter passes every field it finds to __init__.
        init_values = (
            {k: v for k, v in value.items() if k not in non_init} if non_init else value
        )
        # A value the converter can't rebuild, such as a dataclass with a
        # Protocol field, keeps its plain JSON form, as before.
        restored = _convert(converter, init_values, cls)
        if restored is _MISSING:
            return value
        # Restore saved init=False fields with their declared types.
        try:
            hints = get_type_hints(cls) if non_init else {}
        except Exception:
            hints = {}
        for name in non_init:
            if name in value:
                saved = _convert(converter, value[name], hints.get(name, Any))
                saved = value[name] if saved is _MISSING else saved
                object.__setattr__(restored, name, saved)
        return restored


def wrap_activity(
    func: Callable,
    *,
    context_schema: type[Any] | None = None,
    context_schemas: dict[str, type[Any]] | None = None,
    value_types: ValueTypes | None = None,
    streaming_topic: str | None = None,
    streaming_batch_interval: timedelta = timedelta(milliseconds=100),
    # Harness: builds the live observer for calls that carry a ``stream_to`` token.
    observer_factory: ObserverFactory | None = None,
) -> Callable[[ActivityInput], Awaitable[ActivityOutput]]:
    """Wrap a function as a Temporal activity that handles LangGraph config and interrupts."""
    accepts_runtime = "runtime" in signature(func).parameters
    known = value_types if value_types is not None else ValueTypes()

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
            values = {"args": list(input.args), "kwargs": input.kwargs}
            if input.value_types:
                values = known.restore(
                    values, input.value_types, activity.payload_converter()
                )
            args = values["args"]
            kwargs = dict(values["kwargs"])
            if accepts_runtime:
                kwargs["runtime"] = runtime

            try:
                # Harness: a call carrying a routing token hands its LangChain callback
                # events to the observer the factory builds from it.
                async with select_observer(
                    factory=observer_factory,
                    token=input.stream_to,
                    batch_interval=streaming_batch_interval,
                ) as observer, _observing(observer):
                    if iscoroutinefunction(func):
                        result = await func(*args, **kwargs)
                    else:
                        result = await asyncio.to_thread(func, *args, **kwargs)
                # A top-level Command keeps its own field for older Workflows.
                output = (
                    {"langgraph_command": _command_to_payload(result)}
                    if isinstance(result, Command)
                    else {"result": result}
                )
                result_types: list[Any] = []
                output = known.encode(output, [], result_types)
                return ActivityOutput(**output, value_types=result_types or None)
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
    value_types: ValueTypes | None = None,
    **execute_activity_kwargs: Any,
) -> Callable[..., Any]:
    """Wrap an activity function to be called via workflow.execute_activity with caching."""
    known = value_types if value_types is not None else ValueTypes()

    def output_value(result: Any, command: Any, result_types: list[Any] | None) -> Any:
        output = {"result": result, "langgraph_command": command}
        if result_types:
            output = known.restore(output, result_types, workflow.payload_converter())
        if output["langgraph_command"] is not None:
            return _command_from_payload(output["langgraph_command"])
        return output["result"]

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
        # Harness: the run's stream route, if any, gives this call its routing token.
        configurable = (var_child_runnable_config.get() or {}).get("configurable") or {}
        stream_route = configurable.get(STREAM_ROUTE_KEY)
        stream_to = stream_route() if stream_route is not None else None
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
        # is never mistaken for a Command. Value types go under "<key>:types",
        # so older Workflows still find the plain entry.
        if task_id:
            found_command, command = cache_lookup(f"{key}:command")
            found_result, result = cache_lookup(key)
            if found_command or found_result:
                _, result_types = cache_lookup(f"{key}:types")
                return output_value(
                    None if found_command else result, command, result_types
                )

        arg_types: list[Any] = []
        values = known.encode({"args": args, "kwargs": kwargs}, [], arg_types)
        input = ActivityInput(
            args=tuple(values["args"]),
            kwargs=values["kwargs"],
            langgraph_config=langgraph_config,
            value_types=arg_types or None,
            stream_to=stream_to,
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

        # Store in cache for future continue-as-new cycles.
        if task_id:
            if output.langgraph_command is not None:
                cache_put(f"{key}:command", output.langgraph_command)
            else:
                cache_put(key, output.result)
            if output.value_types:
                cache_put(f"{key}:types", output.value_types)

        return output_value(output.result, output.langgraph_command, output.value_types)

    return wrapper


# Harness: the bridge from LangChain callbacks to a StreamObserver.


@asynccontextmanager
async def _observing(observer: Any) -> AsyncIterator[None]:
    """Hand the LangChain callback events of the code run inside to ``observer``.

    Events reach ``observer.on_event`` in order, from a drain task on the activity's
    loop, so a sync node running on a thread is observed the same way.
    """
    if observer is None:
        yield
        return
    bridge = _ObserverCallbacks(asyncio.get_running_loop())
    drain = asyncio.create_task(bridge.drain(observer))
    config = var_child_runnable_config.get() or {}
    callbacks = config.get("callbacks")
    if callbacks is None:
        callbacks = [bridge]
    elif isinstance(callbacks, list):
        callbacks = [*callbacks, bridge]
    else:
        callbacks = callbacks.copy()
        callbacks.add_handler(bridge)
    token = var_child_runnable_config.set({**config, "callbacks": callbacks})
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)
        bridge.close()
        await drain


class _ObserverCallbacks(BaseCallbackHandler):
    """LangChain callbacks as raw events: dicts shaped like ``astream_events`` v2 events.

    ``event`` is ``on_chain_start``, ``on_tool_start``, ``on_chat_model_start``,
    ``on_chat_model_stream`` (``data["chunk"]``, the message chunk),
    ``on_chat_model_end`` (``data["output"]``, the message) or ``on_chat_model_error``
    (``data["error"]``); each carries ``run_id``, ``parent_run_id``, ``tags`` and
    ``metadata``.
    """

    run_inline = True  # called in order on the caller's thread, never in an executor
    _DONE = object()

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[Any] = asyncio.Queue()

    async def drain(self, observer: Any) -> None:
        while (event := await self._queue.get()) is not self._DONE:
            await observer.on_event(event)

    def close(self) -> None:
        self._put(self._DONE)

    def _put(self, item: Any) -> None:
        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._queue.put_nowait(item)
        else:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)

    def _emit(self, event: str, kwargs: dict[str, Any], **data: Any) -> None:
        parent = kwargs.get("parent_run_id")
        self._put(
            {
                "event": event,
                "run_id": str(kwargs["run_id"]),
                "parent_run_id": None if parent is None else str(parent),
                "tags": list(kwargs.get("tags") or []),
                "metadata": dict(kwargs.get("metadata") or {}),
                "data": data,
            }
        )

    # Implementing these makes LangChain stream a model call it would otherwise invoke.
    def tap_output_aiter(self, _run_id: Any, output: Any) -> Any:
        return output

    def tap_output_iter(self, _run_id: Any, output: Any) -> Any:
        return output

    def on_chain_start(self, serialized: Any, inputs: Any, **kwargs: Any) -> None:
        self._emit("on_chain_start", kwargs)

    def on_tool_start(self, serialized: Any, input_str: Any, **kwargs: Any) -> None:
        self._emit("on_tool_start", kwargs)

    def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> None:
        self._emit("on_chat_model_start", kwargs)

    def on_llm_new_token(self, token: Any, *, chunk: Any = None, **kwargs: Any) -> None:
        if isinstance(chunk, ChatGenerationChunk):
            self._emit("on_chat_model_stream", kwargs, chunk=chunk.message)

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        generation = response.generations[0][0] if response.generations else None
        self._emit("on_chat_model_end", kwargs, output=getattr(generation, "message", None))

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self._emit("on_chat_model_error", kwargs, error=error)
