# ABOUTME: The harness's own module beside the vendored LangGraph plugin; the plugin modules never
# import it. Holds the live translator (`LangGraphStreamObserver`) that folds a node's LangChain
# callback events into the harness turn-stream vocabulary, the observer factory and stream route
# that send them to the in-flight turn, `harness_tool_call`, which puts every ToolNode call under the
# harness approval policy and tool lifecycle, and `graph` / `entrypoint`, the plugin's lookups that
# bind a registered graph to an agent's runner when given one.

"""Harness integration for LangGraph.

Build the vendored plugin with the harness's seams on the worker:

>>> from temporal_agent_harness.ai_sdks.langgraph import (
...     LangGraphPlugin, harness_observer_factory, harness_tool_call,
... )
>>> plugin = LangGraphPlugin(
...     graphs={"weather": build_graph()},
...     awrap_tool_call=harness_tool_call,
...     observer_factory=harness_observer_factory,
... )

and compile the graph once in ``@agent.init`` with ``graph(name, self._runner)``, passing the
agent's runner; invoke it per message as any LangGraph code would, with your own ``thread_id``. The chat
models its activity nodes call then stream ``reply_delta`` / ``thought_summary`` /
``tool_requested`` and ``model_interaction_started`` / ``…_ended`` onto the turn stream live, and
every ``ToolNode`` call runs through ``runner.run_tool`` with the agent's approval policy,
``tool_start`` / ``tool_end`` / ``tool_error`` and steering. LangGraph interrupts are refused:
get input from a person or client with an ``@agent.callback_tool_defn`` tool.

.. warning::
    This integration is experimental and may change in future versions.
"""

# pyright: reportMissingTypeStubs=false

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from inspect import iscoroutinefunction, signature
from typing import Any, Self

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

_INSTALL_MESSAGE = (
    "LangGraph support requires the optional `langgraph` extra. "
    "Install it with `uv sync --extra langgraph` or "
    "`pip install 'temporal-agent-harness[langgraph]'`."
)

try:
    from langchain_core.callbacks import AsyncCallbackHandler
    from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
    from langchain_core.messages.ai import UsageMetadata
    from langgraph.config import get_config
    from langgraph.constants import TAG_NOSTREAM
    from langgraph.errors import GraphBubbleUp, GraphInterrupt
    from langgraph.graph import StateGraph
    from langgraph.prebuilt import ToolNode
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.pregel import Pregel
    from langgraph.types import Command
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise RuntimeError(_INSTALL_MESSAGE) from exc

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError, is_cancelled_exception

from temporal_agent_harness.ai_sdks.langgraph import _plugin
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
from temporal_agent_harness.harness.agent_workflow import (
    _RUNNER_ATTR,
    AgentWorkflowRunner,
    TurnEventPublisher,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext

__all__ = [
    "LANGGRAPH_INTERRUPT_NOT_SUPPORTED",
    "entrypoint",
    "graph",
    "harness_observer_factory",
    "harness_tool_call",
]

_HARNESS_TOOL_ATTRS = ("__agent_tool__", "__agent_activity_tool__")

LANGGRAPH_INTERRUPT_NOT_SUPPORTED = "LangGraphInterruptNotSupported"
_INTERRUPT_REFUSAL = (
    "LangGraph interrupts are not supported in a harness agent: nothing would resume the "
    "paused graph. To get input from a person or a client, declare an "
    "@agent.callback_tool_defn tool and give it to the graph's ToolNode; its call waits in "
    "the workflow for the answer. To gate tool calls on a person's approval, use the agent's "
    "tool approval policy."
)

# The run-config key that carries an agent's binding to its graph runs.
_BINDING_KEY = "__harness_langgraph"


# ---------------------------------------------------------------------------
# Live streaming: a node's LangChain callback events -> harness turn vocabulary
# ---------------------------------------------------------------------------


class _HarnessStreamToken(BaseModel):
    """The routing token the harness hands each activity call of a harness graph.

    The in-flight turn's :class:`TurnStreamContext`. It rides the activity input as an
    untyped field and arrives in the activity as a plain dict.
    """

    context: TurnStreamContext


def _harness_stream_route(
    runner: AgentWorkflowRunner,
) -> Callable[[], _HarnessStreamToken | None]:
    """The ``stream_route`` that sends a graph's activity calls to ``runner``'s current turn.

    The plugin calls the route in workflow code before each activity call. It returns
    ``None`` outside a turn, and the call then runs without live streaming.
    """

    def route() -> _HarnessStreamToken | None:
        context = runner.current_stream_context
        if context is None:
            return None
        return _HarnessStreamToken(context=context)

    return route


def harness_observer_factory(
    token: Any, *, batch_interval: timedelta | None = None
) -> LangGraphStreamObserver:
    """Turn an activity call's routing token into a fresh :class:`LangGraphStreamObserver`.

    Wired onto the plugin as ``LangGraphPlugin(observer_factory=...)``.
    """
    tok = (
        token
        if isinstance(token, _HarnessStreamToken)
        else _HarnessStreamToken.model_validate(token)
    )
    return LangGraphStreamObserver(tok.context, batch_interval=batch_interval)


class LangGraphStreamObserver:
    """Translate one activity call's chat-model calls into harness turn events, live.

    One instance per call, so its per-call state never mixes across concurrent calls on a
    shared worker. ``__aenter__`` opens a :class:`TurnEventPublisher` for the turn and starts
    heartbeating, so a ``heartbeat_timeout`` notices a lost worker without timing out a long
    stream; ``__aexit__`` stops both, and fails a model error with a 4xx ``status_code``
    other than 408, 409 and 429 as non-retryable, since retrying won't fix it.

    Each chat model the node calls, directly or through a chain, publishes
    ``model_interaction_started``, text as ``reply_delta``, citations as
    ``text_annotation``, reasoning as ``thought_summary``, a ``tool_requested`` for each
    tool call it makes, and ``model_interaction_ended`` with usage. A tool the provider ran
    itself (web search, ...) is reported as a ``tool_requested`` / ``tool_start`` /
    ``tool_end`` span that no approval policy can gate. A call made under a tool run, or
    tagged ``nostream``, publishes nothing.
    """

    def __init__(
        self, context: TurnStreamContext, *, batch_interval: timedelta | None = None
    ) -> None:
        self._context = context
        self._batch_interval = batch_interval
        self._stack: AsyncExitStack | None = None
        self._publisher: TurnEventPublisher | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._models: dict[str, str | None] = {}
        self._blocked: set[str] = set()  # tool runs and everything under them
        self._streamed: set[str] = set()
        self._hosted: dict[str, str] = {}  # provider-run tool calls awaiting their result

    async def __aenter__(self) -> Self:
        self._stack = AsyncExitStack()
        self._publisher = await self._stack.enter_async_context(
            AgentWorkflowRunner.publisher_from_activity(
                self._context, batch_interval=self._batch_interval
            )
        )
        self._heartbeat = asyncio.create_task(_heartbeat())
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        try:
            if self._heartbeat is not None:
                self._heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await self._heartbeat
        finally:
            if self._stack is not None:
                await self._stack.aclose()
                self._stack = None
            self._publisher = None
        if exc is not None and _non_retryable(exc):
            raise ApplicationError(f"Non-retryable model error: {exc}", non_retryable=True) from exc
        return False

    async def on_event(self, event: dict[str, Any]) -> None:
        """Fold one raw callback event into harness vocabulary."""
        pub = self._publisher
        if pub is None:
            return
        kind, run_id, parent = event["event"], event["run_id"], event["parent_run_id"]
        if kind == "on_chain_start":
            if parent in self._blocked:
                self._blocked.add(run_id)
        elif kind == "on_tool_start":
            self._blocked.add(run_id)
        elif kind == "on_chat_model_start":
            if parent in self._blocked or TAG_NOSTREAM in event["tags"]:
                return
            name = event["metadata"].get("ls_model_name")
            model = name if isinstance(name, str) else None
            self._models[run_id] = model
            pub.publish(ModelInteractionStarted(model=model))
        elif kind == "on_chat_model_stream":
            if run_id in self._models:
                self._streamed.add(run_id)
                self._publish_content(pub, event["data"]["chunk"])
        elif kind == "on_chat_model_end":
            if run_id not in self._models:
                return
            message = event["data"].get("output")
            if run_id not in self._streamed and isinstance(message, BaseMessage):
                self._publish_content(pub, message)
            self._streamed.discard(run_id)
            usage = None
            if isinstance(message, AIMessage):
                usage = message.usage_metadata
                for call in message.tool_calls:
                    if call.get("id"):  # harness_tool_call requests a call it has to give an id
                        pub.publish(
                            ToolRequested(
                                tool_id=call["id"],
                                tool_name=call["name"],
                                tool_input=dict(call["args"]),
                            )
                        )
            pub.publish(
                ModelInteractionEnded(model=self._models.pop(run_id), usage=_token_usage(usage))
            )
        elif kind == "on_chat_model_error":
            self._streamed.discard(run_id)
            if run_id in self._models:
                pub.publish(ModelInteractionEnded(model=self._models.pop(run_id)))

    def _publish_content(self, pub: TurnEventPublisher, message: BaseMessage) -> None:
        for block in message.content_blocks:
            if block["type"] == "text":
                if block["text"]:
                    pub.publish(ReplyDelta(text=block["text"]))
                for annotation in block.get("annotations") or ():
                    pub.publish(TextAnnotationDelta(delta=dict(annotation)))
            elif block["type"] == "reasoning" and (text := block.get("reasoning")):
                # Console reads "content", the Nexus connector "text".
                pub.publish(ThoughtSummaryDelta(delta={"text": text, "content": text}))
            elif block["type"] == "server_tool_call" and block["id"] not in self._hosted:
                # A tool the provider ran itself: no approval is possible, so it is
                # reported, not gated. Its result closes the span below.
                self._hosted[block["id"]] = block["name"]
                args = dict(block.get("args") or {})
                pub.publish(
                    ToolRequested(tool_id=block["id"], tool_name=block["name"], tool_input=args)
                )
                pub.publish(
                    ToolStartEvent(tool_id=block["id"], tool_name=block["name"], tool_input=args)
                )
            elif block["type"] == "server_tool_result" and block["tool_call_id"] in self._hosted:
                pub.publish(
                    ToolEndEvent(
                        tool_id=block["tool_call_id"],
                        tool_name=self._hosted.pop(block["tool_call_id"]),
                        tool_output=json.dumps(to_jsonable_python(block.get("output"))),
                    )
                )


async def _heartbeat() -> None:
    """Heartbeat while an observed activity runs; delivers timeouts and cancellation."""
    timeout = activity.info().heartbeat_timeout
    while True:
        activity.heartbeat()
        await asyncio.sleep(timeout.total_seconds() / 2 if timeout else 5)


def _non_retryable(exc: BaseException) -> bool:
    """A 4xx other than 408, 409 and 429 is the caller's error; retrying won't fix it.

    Provider clients (OpenAI, Anthropic, ...) expose the HTTP status as ``status_code``.
    """
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status not in (408, 409, 429)


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


# ---------------------------------------------------------------------------
# Tools: every ToolNode call through run_tool
# ---------------------------------------------------------------------------


@dataclass
class _Binding:
    """An agent's binding to its graph's runs, read by :func:`harness_tool_call`."""

    runner: AgentWorkflowRunner
    injections: Mapping[str, object]
    call_ids: set[str] = field(default_factory=set)


async def harness_tool_call(
    request: ToolCallRequest,
    execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
) -> ToolMessage | Command:
    """Run a ``ToolNode`` tool call through the harness.

    Wired onto the plugin as ``LangGraphPlugin(awrap_tool_call=...)``, which sets it on
    every Graph API ``ToolNode``; pass it yourself on a ``ToolNode`` built inside a
    Functional API task. The node runs with ``execute_in="workflow"``. In a graph bound to
    an agent with ``graph(name, runner)``, each call goes through ``AgentWorkflowRunner.run_tool``, so
    harness approvals, tool events and ``Injected[...]`` parameters apply, and a tool that
    isn't a harness tool is refused. A streamed model call requested each call when the model
    made it; a call the model gave no id, or a repeated one, is requested here under a fresh
    id. Arguments are validated against the tool's schema before the tool runs. Invalid
    arguments and a failed or denied call become an error ``ToolMessage``; cancellation and
    nondeterminism errors propagate. A result carries any steering a message handler queued
    with ``runner.steer`` since the last one. Outside a harness agent, the call runs as
    LangGraph would run it.
    """
    if request.tool is None:
        return await execute(request)
    binding = (get_config().get("configurable") or {}).get(_BINDING_KEY)
    if not isinstance(binding, _Binding):
        if _in_harness_agent():
            raise RuntimeError(
                "a harness agent's graph runs its tools through the harness; compile it from "
                "graph(name, runner) or entrypoint(name, runner)."
            )
        return await execute(request)
    tool: object = getattr(request.tool, "coroutine", None)
    if not _is_harness_coroutine(tool):
        raise TypeError(
            f"{request.tool.name} is not a harness tool; decorate it with "
            "@agent.tool_defn, @agent.activity_tool_defn or @agent.callback_tool_defn."
        )
    runner = binding.runner
    call = request.tool_call
    name = call.get("name", "")
    try:
        # Clients see the provider's id; a missing one, or one this agent has seen, gets a
        # fresh id. A missing one is written back into the AIMessage's dict, which reaches the
        # state only when the node received the state itself, not a Send copy of the call.
        call_id = call["id"] or ""
        minted = not call_id or call_id in binding.call_ids
        if minted:
            call_id = str(workflow.uuid4())
        binding.call_ids.add(call_id)
        if not call["id"]:
            call["id"] = call_id  # the AIMessage's dict, so both messages carry it
        if minted:
            # The model stream already requested a call under the id the model gave.
            runner.publish(ToolRequested(tool_id=call_id, tool_name=name, tool_input=call["args"]))
        args = _schema_args(request.tool.tool_call_schema, call["args"], tool)
        result = await runner.run_tool(call_id, tool, injections=binding.injections, **args)
    except GraphBubbleUp:
        raise
    except Exception as exc:
        # Cancellation and nondeterminism are the workflow's, not the model's.
        if is_cancelled_exception(exc) or isinstance(exc, workflow.NondeterminismError):
            raise
        message: ToolMessage | Command = ToolMessage(
            f"Tool {name!r} failed: {exc}",
            name=name,
            tool_call_id=call.get("id") or "",
            status="error",
        )
    else:
        message = _result_message(result, name, call["id"])
    return _with_steering(runner, call_id, message)


def _is_harness_coroutine(tool: object) -> bool:
    return iscoroutinefunction(tool) and any(
        getattr(tool, attr, False) for attr in _HARNESS_TOOL_ATTRS
    )


def _in_harness_agent() -> bool:
    return workflow.in_workflow() and getattr(workflow.instance(), _RUNNER_ATTR, None) is not None


def _with_steering(
    runner: AgentWorkflowRunner, tool_id: str, message: ToolMessage | Command
) -> ToolMessage | Command:
    """``message`` with any queued steering appended, claimed for this tool call.

    As the other adapters do: a user's mid-turn message (``runner.steer``) rides the next
    tool result the model sees. A ``Command`` result carries none, so the steering waits for
    the next ``ToolMessage``.
    """
    if not isinstance(message, ToolMessage) or not isinstance(message.content, str):
        return message
    steering = runner.claim_steering(tool_id=tool_id)
    if steering is None:
        return message
    return message.model_copy(update={"content": steering.append_to(message.content)})


def _schema_args(schema: object, args: dict[str, Any], tool: object) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
    """Validate the model's arguments and keep only the schema's fields.

    Only schema fields reach the tool, so the model can't set ``Injected[...]`` ones. A
    pydantic schema is validated (defaults filled, types coerced); a dict schema is filtered
    to its declared properties.
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


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


class _RefuseInterrupts(AsyncCallbackHandler):
    """Fails the turn when a node or tool calls ``interrupt()``.

    An interrupt pauses the graph to be resumed with an answer. Under the harness, callback
    tools and the approval policy take that role, so the node fails with a non-retryable
    ``ApplicationError`` instead. A node's error reaches ``on_chain_error`` before it leaves
    the node, here in workflow code, including an activity node's interrupt re-raised there.
    """

    raise_error = True

    async def on_chain_error(self, error: BaseException, **kwargs: Any) -> None:
        if isinstance(error, GraphInterrupt):
            raise ApplicationError(
                _INTERRUPT_REFUSAL, type=LANGGRAPH_INTERRUPT_NOT_SUPPORTED, non_retryable=True
            ) from error


def _run_config(
    runner: AgentWorkflowRunner, injections: Mapping[str, object] | None
) -> dict[str, Any]:
    return {
        "configurable": {_BINDING_KEY: _Binding(runner, dict(injections or {}))},
        "callbacks": [_RefuseInterrupts()],
    }


class _HarnessGraph:
    """A registered graph bound to an agent: ``compile`` it as the builder, once."""

    def __init__(self, routed: Any, config: dict[str, Any]) -> None:
        self._routed = routed
        self._config = config

    def compile(self, **kwargs: Any) -> Pregel[Any, Any, Any, Any]:
        for key in ("interrupt_before", "interrupt_after"):
            if kwargs.get(key):
                raise ValueError(
                    f"{key} pauses the graph, and nothing resumes it in a harness agent; "
                    "ask the user through a callback tool instead."
                )
        builder: StateGraph[Any, Any, Any, Any] = self._routed.builder
        # The plugin sets the wrapper at registration; ToolNode keeps it in a private field.
        ungated = [
            name
            for name, node in builder.nodes.items()
            if isinstance(node.runnable, ToolNode)
            and node.runnable._awrap_tool_call is not harness_tool_call
        ]
        if ungated:
            raise ValueError(
                f"ToolNodes {ungated} don't run their calls through harness_tool_call; build the "
                "worker's plugin with LangGraphPlugin(..., awrap_tool_call=harness_tool_call) and "
                "leave each ToolNode's own awrap_tool_call unset."
            )
        plain = sorted(
            tool_name
            for node in builder.nodes.values()
            if isinstance(node.runnable, ToolNode)
            for tool_name, tool in node.runnable.tools_by_name.items()
            if not _is_harness_coroutine(getattr(tool, "coroutine", None))
        )
        if plain:
            raise TypeError(
                f"Tools {plain} are not harness tools; a harness graph's ToolNodes take only "
                "@agent.tool_defn, @agent.activity_tool_defn and @agent.callback_tool_defn tools."
            )
        return self._routed.compile(**kwargs).with_config(self._config)


def _require_runner(runner: object) -> None:
    if runner is not None and not isinstance(runner, AgentWorkflowRunner):
        raise TypeError(
            f"the second argument is the agent's AgentWorkflowRunner, not {type(runner).__name__}; "
            "pass a task cache as cache=..."
        )


def graph(
    name: str,
    runner: AgentWorkflowRunner | None = None,
    cache: dict[str, Any] | None = None,
    *,
    injections: Mapping[str, object] | None = None,
) -> StateGraph[Any, Any, Any, Any] | _HarnessGraph:
    """The registered graph ``name``; given ``runner``, bound to that agent.

    Without ``runner`` this is the plugin's own lookup. With it, ``compile`` the result as
    the builder, once, in ``@agent.init``: the compiled graph is plain LangGraph, invoked per
    message with the checkpointer and ``thread_id`` you choose. Its activity calls stream
    onto the turn in flight (with the plugin's ``observer_factory``), its ``ToolNode`` calls
    run through the harness with ``injections`` for their ``Injected[...]`` parameters (with
    the plugin's ``awrap_tool_call``), and an ``interrupt()`` in it is refused. Compiling
    raises if a ``ToolNode`` isn't gated or holds a tool that isn't a harness tool, or if
    ``interrupt_before`` / ``interrupt_after`` is set. ``cache`` is the plugin's
    continue-as-new task cache.
    """
    _require_runner(runner)
    if runner is None:
        return _plugin.graph(name, cache)
    routed = _plugin.graph(name, cache, stream_route=_harness_stream_route(runner))
    return _HarnessGraph(routed, _run_config(runner, injections))


def entrypoint(
    name: str,
    runner: AgentWorkflowRunner | None = None,
    cache: dict[str, Any] | None = None,
    *,
    injections: Mapping[str, object] | None = None,
) -> Pregel[Any, Any, Any, Any]:
    """The registered entrypoint ``name``; given ``runner``, bound as :func:`graph` binds a graph.

    An entrypoint is built once at import, so give each workflow its own checkpointer:
    ``.copy({"checkpointer": InMemorySaver()})``. A ``ToolNode`` built inside a task takes
    ``awrap_tool_call=harness_tool_call`` itself.
    """
    _require_runner(runner)
    if runner is None:
        return _plugin.entrypoint(name, cache)
    pregel = _plugin.entrypoint(name, cache, stream_route=_harness_stream_route(runner))
    return pregel.with_config(_run_config(runner, injections))
