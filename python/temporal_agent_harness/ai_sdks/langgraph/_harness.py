# ABOUTME: The harness's LangGraph integration beside the vendored plugin: context schema,
# ABOUTME: model-stream wrapper, ToolNode gate, and the turn binding graph()/entrypoint() apply.

"""Harness integration for LangGraph, via the vendored ``temporalio-langgraph`` plugin (``ai_sdks.langgraph``).

The harness owns the session, the turn event stream, tool approvals and clients. LangGraph owns
the inner loop, made durable by ``temporalio-langgraph``. This module connects them:

- ``graph(name, runner, injections=...)`` (and ``entrypoint`` for the Functional API) is the
  plugin's lookup bound to the harness: the compiled graph runs with a :class:`HarnessContext`,
  the graph's ``context_schema``, which carries the harness runner and tool injections inside the
  workflow; the plugin sends the turn in flight's stream context to activity nodes beside it.
  Everything else, the checkpointer and its ``thread_id`` included, is the caller's, as in any
  LangGraph code.
- An activity node with ``"stream_to_harness": True`` in its metadata (or a task's
  ``activity_options``) has its chat-model calls publish ``model_interaction_started``,
  ``reply_delta``, ``thought_summary`` and ``model_interaction_ended`` (with token usage) onto
  the turn stream; ``"stream_batch_interval"`` sets how often buffered deltas reach the
  workflow. The plugin wraps the node at registration. It installs a
  :class:`LangGraphStreamHandler`, the LangChain callback handler that does the publishing,
  the counterpart of the OpenAI Agents and Pydantic AI stream observers.
- :func:`harness_tool_call` is a ``ToolNode`` ``awrap_tool_call`` that runs each call through
  ``AgentWorkflowRunner.run_tool``, so approvals, tool events and ``Injected[...]`` parameters
  apply.
- :func:`adopt_prebuilt` makes a prebuilt graph such as ``create_react_agent``'s run on the plugin
  with the three above applied.

Build the graph with the harness context and the plugin's activity metadata:

>>> from datetime import timedelta
>>> from langgraph.checkpoint.memory import InMemorySaver
>>> from langgraph.graph import END, START, MessagesState, StateGraph
>>> from langgraph.prebuilt import ToolNode, tools_condition
>>> from temporal_agent_harness.ai_sdks.langgraph import (
...     HarnessContext, LangGraphPlugin, graph, harness_tool_call,
... )
>>> from temporalio.common import RetryPolicy
>>> model = ChatOpenAI(model="gpt-5.1").bind_tools([get_weather])  # get_weather: @agent.tool_defn()
>>> async def call_model(state: MessagesState) -> dict:
...     return {"messages": [await model.ainvoke(state["messages"])]}
>>> async def route(state: dict) -> str:  # async wrapper: sync routers can't run in a workflow
...     return tools_condition(state)
>>> g = StateGraph(MessagesState, context_schema=HarnessContext)
>>> g.add_node("agent", call_model, metadata={
...     "execute_in": "activity",
...     "stream_to_harness": True,
...     "start_to_close_timeout": timedelta(minutes=2),
...     "heartbeat_timeout": timedelta(seconds=10),
...     "retry_policy": RetryPolicy(maximum_attempts=3),
... })
>>> g.add_node("tools", ToolNode([get_weather]), metadata={"execute_in": "workflow"})
>>> g.add_edge(START, "agent")
>>> g.add_conditional_edges("agent", route, ["tools", END])
>>> g.add_edge("tools", "agent")

In the agent, compile once in ``@agent.init`` and run one turn per message on the conversation's
thread:

>>> self._graph = graph("weather", self._runner).compile(checkpointer=InMemorySaver())
...
>>> config = {"recursion_limit": 25, "configurable": {"thread_id": workflow.info().workflow_id}}
>>> result = await self._graph.ainvoke({"messages": [HumanMessage(message.text)]}, config)
>>> reply = result["messages"][-1].text

On the client, register ``LangGraphPlugin`` before ``AgentHarnessPlugin`` so the harness's data
converter wins; workers built from the client pick both up:

>>> client = await Client.connect("localhost:7233", plugins=[
...     LangGraphPlugin(graphs={"weather": g}),
...     AgentHarnessPlugin(tools=[get_weather]),
... ])

Behavior and limits (the rest, with the Functional API recipe and the prebuilt-agent rules,
is in ``python/examples/langgraph_hello/README.md``):

- Only async nodes and routers run in the workflow: LangGraph runs sync functions in a thread
  pool, which workflows forbid. Wrap prebuilt sync routers such as ``tools_condition``, as above.
- A ``stream_to_harness`` node streams the chat models it calls, directly or through a chain such
  as ``prompt | model``, with citations as ``text_annotation`` and a tool the provider ran itself
  (web search, ...) as a ``tool_requested`` / ``tool_start`` / ``tool_end`` span that no approval
  policy can gate. A call made under a tool run, or tagged ``nostream``, publishes nothing:
  tag a structured-output chain (``model.with_structured_output(...).with_config(tags=["nostream"])``)
  or any model call that is not the reply, or its text streams as ``reply_delta``. A model that
  doesn't stream (for example ``disable_streaming=True``) publishes its whole reply when it finishes. The plugin's
  own ``streaming_topic`` (``get_stream_writer()``) is bypassed: its custom-mode stream carries
  whatever a node writes, not the model's tokens, which the harness needs as ``reply_delta``;
  a node may still write to it alongside.
- A streamed node heartbeats while it runs, so give the model node a
  ``heartbeat_timeout``: a lost worker is then retried after it, not after
  ``start_to_close_timeout``. Cancelling the workflow doesn't cancel the running activity; it
  stops when its next heartbeat fails.
- Give every activity node a ``retry_policy``: Temporal retries an activity forever by default,
  and each attempt re-streams. A streamed node fails a model error carrying a 4xx
  ``status_code`` other than 408, 409 and 429 as non-retryable, so a bad key fails the turn once.
- Ask the user through a harness callback tool (``@agent.callback_tool_defn``), not LangGraph's
  ``interrupt()``: the harness holds the turn open until the client answers, so one message is
  one graph run. An ``interrupt()`` pauses the graph instead, and nothing resumes it.
- Pass a ``recursion_limit`` in the invoke config: a model can re-call a denied tool
  indefinitely, and reaching the limit fails the turn.

.. warning::
    This integration is experimental and may change in future versions.
"""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar
from contextlib import suppress
from datetime import timedelta
from functools import wraps
from inspect import iscoroutinefunction, signature
from typing import Any, ParamSpec, Protocol, TypeAlias, TypeVar, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pydantic_core import to_jsonable_python

_INSTALL_MESSAGE = (
    "LangGraph support requires the optional `langgraph` extra. "
    "Install it with `uv sync --extra langgraph` or "
    "`pip install 'temporal-agent-harness[langgraph]'`."
)

try:
    from langchain_core.callbacks import (
        AsyncCallbackHandler,
        BaseCallbackHandler,
        BaseCallbackManager,
        Callbacks,
    )
    from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
    from langchain_core.messages.ai import UsageMetadata
    from langchain_core.outputs import ChatGenerationChunk, GenerationChunk, LLMResult
    from langchain_core.runnables import RunnableConfig
    from langchain_core.runnables.config import run_in_executor, var_child_runnable_config
    from langgraph._internal._constants import CONFIG_KEY_RUNTIME
    from langgraph._internal._runnable import RunnableCallable
    from langgraph.constants import TAG_NOSTREAM
    from langgraph.errors import GraphBubbleUp
    from langgraph.graph import StateGraph
    from langgraph.graph.state import CompiledStateGraph
    from langgraph.prebuilt import ToolNode
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.pregel import Pregel
    from langgraph.runtime import Runtime
    from langgraph.types import Command
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise RuntimeError(_INSTALL_MESSAGE) from exc

from temporal_agent_harness.ai_sdks.langgraph._task_cache import set_task_cache
from temporal_agent_harness.harness import AgentWorkflowRunner
from temporal_agent_harness.harness.agent_protocol import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    TextAnnotationDelta,
    ThoughtSummaryDelta,
    TokenUsage,
    ToolEndEvent,
    ToolRequested,
    ToolStartEvent,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError, is_cancelled_exception

_HARNESS_TOOL_ATTRS = ("__agent_tool__", "__agent_activity_tool__")

__all__ = [
    "HarnessContext",
    "LangGraphStreamHandler",
    "adopt_prebuilt",
    "harness_tool_call",
]

P = ParamSpec("P")
R = TypeVar("R")
T = TypeVar("T")

_StreamEvent: TypeAlias = (
    ModelInteractionStarted
    | ModelInteractionEnded
    | ReplyDelta
    | TextAnnotationDelta
    | ThoughtSummaryDelta
    | ToolRequested
    | ToolStartEvent
    | ToolEndEvent
)


class _Publisher(Protocol):
    def publish(self, event: _StreamEvent, /) -> None: ...


class HarnessContext(BaseModel):
    """LangGraph runtime context for a harness agent's graph.

    Declare it as the graph's ``context_schema``; ``graph()`` makes one. It is
    workflow-side state: ``runner`` and ``injections`` serve
    :func:`harness_tool_call` and neither crosses into an activity node (the
    plugin sends the turn's stream context beside the context instead). A
    subclass may add fields, passed as keywords to ``graph()``, but must then
    be the graph's declared ``context_schema``: activity nodes reject an
    unregistered context type.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    runner: AgentWorkflowRunner | None = Field(default=None, exclude=True, repr=False)
    injections: dict[str, object] = Field(
        default_factory=dict, exclude=True, repr=False
    )
    _call_ids: set[str] = PrivateAttr(default_factory=set)


activity_turn: ContextVar[TurnStreamContext | None] = ContextVar("activity_turn", default=None)
"""The harness turn an activity node runs in: the plugin sets it from the runner's turn in
flight on the workflow side and restores it on the activity side, where there is no runner."""


def _turn_config(
    schema: object,
    runner: AgentWorkflowRunner,
    injections: Mapping[str, object] | None,
    fields: Mapping[str, object],
) -> RunnableConfig:
    if not (isinstance(schema, type) and issubclass(schema, HarnessContext)):
        raise TypeError(f"the graph's context_schema must be a HarnessContext, got {schema!r}")
    context = schema(runner=runner, injections=dict(injections or {}), **fields)
    # graph() seeds the plugin's continue-as-new task cache on whichever context
    # @agent.init ran in, where it would retain every node result for the
    # workflow's lifetime. The harness never continues-as-new: run without one.
    set_task_cache(None)
    # The context rides as the run's parent runtime, which ainvoke(context=None) inherits.
    return {"configurable": {CONFIG_KEY_RUNTIME: Runtime(context=context)}}


def adopt_prebuilt(
    compiled: CompiledStateGraph[Any, Any, Any, Any],  # pyright: ignore[reportExplicitAny]
    activity_options: dict[str, dict[str, Any]],  # pyright: ignore[reportExplicitAny]
) -> StateGraph[Any, Any, Any, Any]:  # pyright: ignore[reportExplicitAny]
    """Make a prebuilt graph, such as ``create_react_agent``'s, run on the plugin.

    Returns the graph's builder to register with ``LangGraphPlugin``.
    ``activity_options`` maps each model-calling node to its activity options
    (timeouts, retry policy, summary): those nodes run as activities with
    ``stream_to_harness`` set.
    Every other node runs in the workflow, each ``ToolNode`` without its own
    ``awrap_tool_call`` goes through :func:`harness_tool_call`, closure nodes get
    the module-level identity the plugin keys them by, and sync routers get the
    async twin the workflow can run. Build the prebuilt with
    ``context_schema=HarnessContext``.
    """
    builder = compiled.builder
    for name, node in builder.nodes.items():
        runnable = node.runnable
        if not isinstance(runnable, RunnableCallable):
            raise TypeError(f"Node {name!r} is not a RunnableCallable")
        if name in activity_options:
            fn = runnable.afunc or runnable.func
            if fn is None:
                raise ValueError(f"Node {name!r} has no function")
            runnable.afunc = _module_level(fn, name)
            runnable.func = None  # the plugin takes afunc only when func is gone
            node.metadata = {
                "execute_in": "activity",
                "stream_to_harness": True,
                **activity_options[name],
            }
        else:
            node.metadata = {"execute_in": "workflow", **(node.metadata or {})}
    for branches in builder.branches.values():
        for branch in branches.values():
            path = branch.path
            # LangGraph gives a sync router an afunc that runs it in the executor.
            if (
                isinstance(path, RunnableCallable)
                and path.func
                and getattr(path.afunc, "func", None) is run_in_executor
            ):
                path.afunc = _async(path.func)
    return builder


def _module_level(
    fn: Callable[..., Any], name: str  # pyright: ignore[reportExplicitAny]
) -> Callable[..., Awaitable[Any]]:  # pyright: ignore[reportExplicitAny]
    """A function the plugin can key by qualname, forwarding what ``fn`` accepts.

    The activity passes ``runtime`` only to a signature that names it and
    ``config`` whenever the node accepts it; ``fn`` may take either or neither.
    """
    wants = signature(fn).parameters
    call = fn if iscoroutinefunction(fn) else None

    async def node(state: object, *, runtime: object = None, config: object = None) -> object:
        extra = {k: v for k, v in (("runtime", runtime), ("config", config)) if k in wants}
        if call is not None:
            return await call(state, **extra)
        return await asyncio.to_thread(fn, state, **extra)

    node.__name__ = name
    node.__qualname__ = f"adopt_prebuilt.{name}"
    node.__module__ = __name__
    return node


def _async(fn: Callable[..., Any]) -> Callable[..., Awaitable[Any]]:  # pyright: ignore[reportExplicitAny]
    """An async twin: the workflow can't run a sync router in a thread pool."""

    async def twin(*args: object, **kwargs: object) -> object:
        return fn(*args, **kwargs)

    return twin


async def harness_tool_call(
    request: ToolCallRequest,
    execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
) -> ToolMessage | Command:
    """Run a ``ToolNode`` tool call through the harness.

    The plugin sets it as the ``awrap_tool_call`` of every ``ToolNode`` in a
    graph whose ``context_schema`` is a :class:`HarnessContext`; pass it
    yourself on a ``ToolNode`` built inside a Functional API task. The node
    runs with ``execute_in="workflow"``. Each call goes through
    ``AgentWorkflowRunner.run_tool``, so harness approvals, tool events and
    ``Injected[...]`` parameters apply. A streamed model node publishes
    ``tool_requested`` when the model makes the call; a call the model gave no
    id, or a repeated one, is requested here under a fresh id.
    Arguments are validated against the tool's schema before the tool runs.
    Invalid arguments and a failed or denied
    call become an error ``ToolMessage``; cancellation and nondeterminism
    errors propagate.
    """
    if request.tool is None:
        return await execute(request)
    tool: object = getattr(request.tool, "coroutine", None)
    if not iscoroutinefunction(tool) or not any(
        getattr(tool, attr, False) for attr in _HARNESS_TOOL_ATTRS
    ):
        raise TypeError(
            f"{request.tool.name} is not a harness tool; decorate it with "
            "@agent.tool_defn or @agent.activity_tool_defn."
        )
    context = cast(object, request.runtime.context)  # typed None by LangGraph
    if not isinstance(context, HarnessContext) or context.runner is None:
        raise RuntimeError(
            "harness_tool_call needs a HarnessContext with a runner; pass the runner to "
            "graph() in an agent workflow."
        )
    call = request.tool_call
    name = call.get("name", "")
    try:
        # Clients see the provider's id; a missing one, or one this agent has seen, gets a
        # fresh id. A missing one is written back into the AIMessage's dict, which reaches the
        # state only when the node received the state itself, not a Send copy of the call.
        call_id = call["id"] or ""
        minted = not call_id or call_id in context._call_ids
        if minted:
            call_id = str(workflow.uuid4())
        context._call_ids.add(call_id)
        if not call["id"]:
            call["id"] = call_id  # the AIMessage's dict, so both messages carry it
        if minted:
            # The model stream already requested a call under the id the model gave.
            context.runner.publish(
                ToolRequested(tool_id=call_id, tool_name=name, tool_input=call["args"])
            )
        args = _schema_args(request.tool.tool_call_schema, call["args"], tool)
        result = await context.runner.run_tool(
            call_id, tool, injections=context.injections, **args
        )
    except GraphBubbleUp:
        raise
    except Exception as exc:
        # Cancellation and nondeterminism are the workflow's, not the model's.
        if is_cancelled_exception(exc) or isinstance(exc, workflow.NondeterminismError):
            raise
        return ToolMessage(
            f"Tool {name!r} failed: {exc}",
            name=name,
            tool_call_id=call.get("id") or "",
            status="error",
        )
    return _result_message(result, name, call["id"])


def _schema_args(schema: object, args: dict[str, Any], tool: object) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
    """Validate the model's arguments and keep only the schema's fields.

    Only schema fields reach the tool, so the model can't set ``Injected[...]``
    ones. A pydantic schema is validated (defaults filled, types coerced); a
    dict schema is filtered to its declared properties.
    """
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        validated = schema.model_validate(args)
        params = signature(tool).parameters
        return {
            **{k: v for k, v in (validated.model_extra or {}).items() if k not in params},
            **{key: getattr(validated, key) for key in schema.model_fields},
        }
    declared = schema.get("properties", {}) if isinstance(schema, dict) else {}
    return {k: v for k, v in args.items() if k in declared}


def _result_message(result: object, name: str, call_id: str) -> ToolMessage | Command:
    """A tool's return value as the model sees it: itself, or JSON in a ``ToolMessage``."""
    if isinstance(result, (ToolMessage, Command)):
        return result
    try:
        content = (
            result
            if isinstance(result, str)
            else json.dumps(to_jsonable_python(result, fallback=_describe))
        )
    except (TypeError, ValueError):
        content = _describe(result)
    return ToolMessage(content, name=name, tool_call_id=call_id)


def _describe(value: object) -> str:
    # Only the type: str() can leak private state or a memory address to the model.
    return f"<{type(value).__qualname__}>"


def _stream_to_harness(
    node: Callable[P, Awaitable[R]], *, batch_interval: timedelta | None = None
) -> Callable[P, Awaitable[R]]:
    """Stream an activity node's chat-model calls onto the harness turn.

    The plugin applies this to a node or task whose options set
    ``"stream_to_harness": True``, with ``"stream_batch_interval"`` as
    ``batch_interval``. The node must be async and run with
    ``execute_in="activity"``. Chat models
    the node calls, directly or through a chain, publish
    ``model_interaction_started``, text as ``reply_delta``, reasoning as
    ``thought_summary`` and ``model_interaction_ended`` with usage. Calls made
    under a tool run, or tagged ``nostream``, are not streamed. Outside a harness
    turn the node runs unchanged. ``batch_interval`` is how often buffered
    deltas are flushed to the workflow (the publisher's default otherwise).
    A model error with a 4xx ``status_code`` other than 408, 409 or 429 fails
    the activity as non-retryable.
    """
    if not iscoroutinefunction(node):
        raise TypeError(f"stream_to_harness needs an async node, got {node!r}")

    @wraps(node)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        turn = activity_turn.get()
        if turn is None:
            return await node(*args, **kwargs)
        if not activity.in_activity():
            raise RuntimeError(
                "stream_to_harness needs an activity node; set execute_in='activity'."
            )
        heartbeat = asyncio.create_task(_heartbeat())
        try:
            async with AgentWorkflowRunner.publisher_from_activity(
                turn, batch_interval=batch_interval
            ) as publisher:
                config = var_child_runnable_config.get() or {}
                token = var_child_runnable_config.set(
                    {
                        **config,
                        "callbacks": _with_handler(
                            config.get("callbacks"), LangGraphStreamHandler(publisher)
                        ),
                    }
                )
                try:
                    return await node(*args, **kwargs)
                except Exception as exc:
                    if _non_retryable(exc):
                        raise ApplicationError(
                            f"Non-retryable model error: {exc}", non_retryable=True
                        ) from exc
                    raise
                finally:
                    var_child_runnable_config.reset(token)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    return wrapper



def _non_retryable(exc: BaseException) -> bool:
    """A 4xx other than 408, 409 and 429 is the caller's error; retrying won't fix it.

    Provider clients (OpenAI, Anthropic, ...) expose the HTTP status as ``status_code``.
    """
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status not in (408, 409, 429)


def _with_handler(
    callbacks: Callbacks, handler: AsyncCallbackHandler
) -> list[BaseCallbackHandler] | BaseCallbackManager:
    """Add ``handler`` to the config's callbacks, a list or a manager."""
    if callbacks is None:
        return [handler]
    if isinstance(callbacks, list):
        return [*callbacks, handler]
    manager = callbacks.copy()
    manager.add_handler(handler)
    return manager


async def _heartbeat() -> None:
    """Heartbeat while a streaming node runs.

    Lets a ``heartbeat_timeout`` detect a lost worker without timing out a
    long stream, and delivers timeouts and cancellation to the activity.
    """
    timeout = activity.info().heartbeat_timeout
    while True:
        activity.heartbeat()
        await asyncio.sleep(timeout.total_seconds() / 2 if timeout else 5)


class LangGraphStreamHandler(AsyncCallbackHandler):
    """A LangChain callback handler that publishes a model call as harness turn events.

    A ``stream_to_harness`` node installs one per run; use it
    directly to stream a model call made outside such a node, by passing it in
    the call's ``config={"callbacks": [...]}`` with any publisher that has
    ``publish(event)``.
    """

    def __init__(self, publisher: _Publisher) -> None:
        self._publisher = publisher
        self._models: dict[UUID, str | None] = {}
        self._blocked: set[UUID] = set()  # tool runs and everything under them
        self._streamed: set[UUID] = set()
        self._hosted: set[str] = set()  # provider-run tool calls awaiting their result
        self._hosted_names: dict[str, str] = {}

    # Satisfy langchain-core's _StreamingCallbackHandler check, which turns streaming on.
    def tap_output_aiter(
        self, _run_id: UUID, output: AsyncIterator[T]
    ) -> AsyncIterator[T]:
        return output

    def tap_output_iter(self, _run_id: UUID, output: Iterator[T]) -> Iterator[T]:
        return output

    async def on_chain_start(
        self,
        serialized: dict[str, object],
        inputs: dict[str, object],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        if parent_run_id in self._blocked:
            self._blocked.add(run_id)

    async def on_tool_start(
        self,
        serialized: dict[str, object],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, object] | None = None,
        inputs: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        self._blocked.add(run_id)

    async def on_chat_model_start(
        self,
        serialized: dict[str, object],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        if parent_run_id in self._blocked or TAG_NOSTREAM in (tags or ()):
            return
        name = (metadata or {}).get("ls_model_name")
        model = name if isinstance(name, str) else None
        self._models[run_id] = model
        self._publisher.publish(ModelInteractionStarted(model=model))

    async def on_llm_new_token(
        self,
        token: object,
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: object,
    ) -> None:
        if run_id not in self._models or not isinstance(chunk, ChatGenerationChunk):
            return
        self._streamed.add(run_id)
        self._publish_content(chunk.message)

    def _publish_content(self, message: BaseMessage) -> None:
        for block in message.content_blocks:
            if block["type"] == "text":
                if block["text"]:
                    self._publisher.publish(ReplyDelta(text=block["text"]))
                for annotation in block.get("annotations") or ():
                    self._publisher.publish(TextAnnotationDelta(delta=dict(annotation)))
            elif block["type"] == "reasoning" and (text := block.get("reasoning")):
                # Console reads "content", the Nexus connector "text".
                self._publisher.publish(
                    ThoughtSummaryDelta(delta={"text": text, "content": text})
                )
            elif block["type"] == "server_tool_call" and block["id"] not in self._hosted:
                # A tool the provider ran itself (web search, ...): no approval is possible,
                # so it is reported, not gated. Its result closes the span below.
                self._hosted.add(block["id"])
                self._hosted_names[block["id"]] = block["name"]
                args = dict(block.get("args") or {})
                self._publisher.publish(
                    ToolRequested(tool_id=block["id"], tool_name=block["name"], tool_input=args)
                )
                self._publisher.publish(
                    ToolStartEvent(tool_id=block["id"], tool_name=block["name"], tool_input=args)
                )
            elif block["type"] == "server_tool_result" and block["tool_call_id"] in self._hosted:
                self._hosted.discard(block["tool_call_id"])
                self._publisher.publish(
                    ToolEndEvent(
                        tool_id=block["tool_call_id"],
                        tool_name=self._hosted_names.pop(block["tool_call_id"], ""),
                        tool_output=json.dumps(to_jsonable_python(block.get("output"))),
                    )
                )

    async def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: object,
    ) -> None:
        if run_id not in self._models:
            return
        message = (
            getattr(response.generations[0][0], "message", None)
            if response.generations
            else None
        )
        if run_id not in self._streamed and isinstance(message, BaseMessage):
            self._publish_content(message)
        self._streamed.discard(run_id)
        usage = None
        if isinstance(message, AIMessage):
            usage = message.usage_metadata
            for call in message.tool_calls:
                if call.get("id"):  # the ToolNode requests a call it has to give an id
                    self._publisher.publish(
                        ToolRequested(
                            tool_id=call["id"], tool_name=call["name"], tool_input=dict(call["args"])
                        )
                    )
        self._publisher.publish(
            ModelInteractionEnded(
                model=self._models.pop(run_id), usage=_token_usage(usage)
            )
        )

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: object,
    ) -> None:
        self._streamed.discard(run_id)
        if run_id in self._models:
            self._publisher.publish(
                ModelInteractionEnded(model=self._models.pop(run_id))
            )


def _token_usage(usage: UsageMetadata | None) -> TokenUsage | None:
    if usage is None:
        return None
    return TokenUsage(
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        total_tokens=usage.get("total_tokens"),
        cached_tokens=(usage.get("input_token_details") or {}).get("cache_read"),
        thought_tokens=(usage.get("output_token_details") or {}).get("reasoning"),
    )
