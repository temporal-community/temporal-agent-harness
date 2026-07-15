# ABOUTME: Harness-owned glue for the vendored OpenAI Agents integration. Holds everything
# harness-specific that the vendored `openai_agents/` package deliberately does NOT know about:
# the live translator (`OpenAIStreamObserver`) that folds raw OpenAI Responses stream events into
# the harness turn-stream vocabulary, the observer factory + `stream_to_provider` that route those
# events to the in-flight turn with zero explicit threading, and `as_openai_agent_tool(s)` adapting
# harness tools onto the SDK. Kept a sibling of the vendored tree (not inside it) so that tree stays
# pristine for future upstream merges. Mirrors the structure of the Gemini plugin's
# `_interactions_activity._StreamEventPublisher`.

"""Harness integration for the OpenAI Agents SDK.

Wire it onto a worker by building the vendored plugin with the harness seam:

>>> from temporal_agent_harness.ai_sdks.openai_agents import (
...     ModelActivityParameters,
...     OpenAIAgentsPlugin,
... )
>>> from temporal_agent_harness.ai_sdks.openai_agents_harness import (
...     harness_observer_factory,
...     stream_to_provider,
... )
>>> plugin = OpenAIAgentsPlugin(
...     model_params=ModelActivityParameters(stream_to_provider=stream_to_provider),
...     observer_factory=harness_observer_factory,
... )

With that in place, an agent's ``Runner.run_streamed(...)`` model calls stream
``reply_delta`` / ``thought_summary`` / ``text_annotation`` / ``tool_requested`` and
``model_interaction_started`` / ``…_ended`` onto the harness turn stream live — the same
vocabulary the Gemini plugin produces.

.. warning::
    Streaming support is experimental and may change in future versions.
"""

from __future__ import annotations

import importlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemAddedEvent,
    ResponseOutputTextAnnotationAddedEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseTextDeltaEvent,
)

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.ai_sdks.integration_helpers import StreamObserver
from temporal_agent_harness.harness.agent_protocol import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    TextAnnotationDelta,
    ThoughtSummaryDelta,
    TokenUsage,
    ToolRequested,
)
from temporal_agent_harness.harness.agent_workflow import (
    AgentWorkflowRunner,
    TurnEventPublisher,
    current_stream_context,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext

if TYPE_CHECKING:
    from agents import Tool
    from agents.items import TResponseStreamEvent

__all__ = [
    "OpenAIStreamObserver",
    "harness_observer_factory",
    "stream_to_provider",
    "as_openai_agent_tool",
    "as_openai_agent_tools",
]

_INSTALL_MESSAGE = (
    "OpenAI Agents SDK support requires the optional `openai-agents` extra. "
    "Install it with `uv sync --extra openai-agents` or "
    "`pip install 'temporal-agent-harness[openai-agents]'`."
)

_HARNESS_TOOL_ATTRS = ("__agent_tool__", "__agent_activity_tool__")


# ---------------------------------------------------------------------------
# Live streaming: raw OpenAI Responses events -> harness turn vocabulary
# ---------------------------------------------------------------------------
#
# The OpenAI Responses stream is a flat sequence of typed events, not the
# step.start/delta/stop lifecycle Gemini uses — so the correlation this observer
# has to maintain is smaller: reply/thought/annotation text arrives already
# framed as its own delta event (publish straight through), and the only stateful
# thing is a custom function call, whose name + call_id arrive on
# ``response.output_item.added`` while its JSON arguments stream over
# ``response.function_call_arguments.delta`` and finalize on
# ``…done``. We consolidate those into ONE ``tool_requested`` keyed by the SDK
# ``call_id`` — the same id ``as_openai_agent_tool`` hands to ``run_tool`` for the
# execution lifecycle, so ``tool_requested`` and the eventual ``tool_start`` /
# ``tool_end`` share a ``tool_id`` (spec §4.4).
#
# As with Gemini, ``run_tool`` OWNS ``tool_start`` / ``tool_end`` / ``tool_error``
# for custom tools; this translator only emits ``tool_requested`` (the model's
# request, as it streams out of the LLM). Hosted/server-side tool spans
# (web_search, file_search, …) are deferred (spec §11) — Monty uses none, and the
# exact event shapes must be pinned against a recorded fixture before we bracket
# them.


class OpenAIStreamObserver:
    """Translate one streamed OpenAI model call into harness turn events, live.

    A :class:`StreamObserver` — one instance per streamed call, so its per-call
    state (function-call arg buffers, captured usage) never bleeds across the
    concurrent calls a shared worker runs. Constructed with the call's
    :class:`TurnStreamContext` and the requested ``model`` id; opens a
    :class:`TurnEventPublisher` bound to that turn on ``__aenter__`` and publishes
    ``model_interaction_started`` THERE — at model-call dispatch, before the first
    event — so the started→ended span measures the true model-call latency
    (time-to-first-token included). ``on_event`` folds each raw event into harness
    vocabulary; ``__aexit__`` closes the bracket with ``…_ended`` carrying usage read
    off the terminal ``response.completed``.
    """

    def __init__(self, context: TurnStreamContext, *, model: str | None = None) -> None:
        self._context = context
        # The requested model id, known at dispatch (from the streaming activity input),
        # so BOTH brackets name it — matching how the Gemini plugin reads the model from
        # the request. Never read from the stream: doing so would delay `started` past the
        # first event and corrupt the measured model-call latency.
        self._model = model
        self._stack: AsyncExitStack | None = None
        self._publisher: TurnEventPublisher | None = None
        # Function-call item id -> (call_id, tool_name), recorded on
        # response.output_item.added and consumed on the matching …arguments.done.
        self._fn_calls: dict[str, tuple[str, str]] = {}
        # Function-call item id -> accumulated JSON-string argument fragments.
        self._arg_buffers: dict[str, str] = {}
        # Token usage, captured off the terminal response.completed for the ended bracket.
        self._usage: TokenUsage | None = None

    async def __aenter__(self) -> OpenAIStreamObserver:
        self._stack = AsyncExitStack()
        self._publisher = await self._stack.enter_async_context(
            AgentWorkflowRunner.publisher_from_activity(self._context)
        )
        # Open the model-interaction span at dispatch, before awaiting any event, so the
        # span duration is the real call latency.
        self._publisher.publish(ModelInteractionStarted(model=self._model))
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool | None:
        try:
            if self._publisher is not None:
                # In a finally-equivalent: always close the started bracket, even if the
                # stream errored (usage then stays None). Published while the
                # publisher/stack is still open.
                self._publisher.publish(
                    ModelInteractionEnded(model=self._model, usage=self._usage)
                )
        finally:
            if self._stack is not None:
                await self._stack.aclose()
                self._stack = None
            self._publisher = None
        # Never swallow a stream error: the batched-collect path owns failures.
        return False

    async def on_event(self, event: "TResponseStreamEvent") -> None:
        """Fold one raw Responses stream event into harness vocabulary."""
        pub = self._publisher
        if pub is None:
            return

        if isinstance(event, ResponseTextDeltaEvent):
            if event.delta:
                pub.publish(ReplyDelta(text=event.delta))
        elif isinstance(event, ResponseReasoningSummaryTextDeltaEvent):
            # Reasoning *summary* text (capability-gated, spec §6). The raw
            # payload is dumped so a consumer can position it like Gemini's.
            pub.publish(
                ThoughtSummaryDelta(delta=event.model_dump(exclude_none=True, mode="json"))
            )
        elif isinstance(event, ResponseOutputTextAnnotationAddedEvent):
            pub.publish(
                TextAnnotationDelta(delta=event.model_dump(exclude_none=True, mode="json"))
            )
        elif isinstance(event, ResponseOutputItemAddedEvent):
            item = event.item
            if isinstance(item, ResponseFunctionToolCall):
                # A custom function call is opening. Record name + call_id keyed
                # by the item id; its args stream next and finalize on …done.
                item_id = item.id or item.call_id
                self._fn_calls[item_id] = (item.call_id, item.name)
                self._arg_buffers.setdefault(item_id, "")
        elif isinstance(event, ResponseFunctionCallArgumentsDeltaEvent):
            if event.delta:
                self._arg_buffers[event.item_id] = (
                    self._arg_buffers.get(event.item_id, "") + event.delta
                )
        elif isinstance(event, ResponseFunctionCallArgumentsDoneEvent):
            self._emit_tool_requested(event, pub)
        elif isinstance(event, ResponseCompletedEvent):
            self._usage = _to_token_usage(getattr(event.response, "usage", None))

    def _emit_tool_requested(
        self,
        event: ResponseFunctionCallArgumentsDoneEvent,
        pub: TurnEventPublisher,
    ) -> None:
        """Publish the consolidated ``tool_requested`` for one custom function call.

        Prefers the done event's authoritative ``arguments`` string, falling back
        to the buffered delta fragments. ``tool_id`` is the SDK ``call_id`` (shared
        with the execution lifecycle in ``run_tool``); if the opening
        ``output_item.added`` was somehow missed, we degrade to the item id and the
        done event's own name rather than dropping the request.
        """
        item_id = event.item_id
        buffered = self._arg_buffers.pop(item_id, "")
        call_id, name = self._fn_calls.pop(item_id, (item_id, event.name))
        raw = event.arguments or buffered
        pub.publish(
            ToolRequested(
                tool_id=call_id,
                tool_name=name,
                tool_input=_parse_tool_args(raw),
            )
        )


def _parse_tool_args(raw: str) -> dict[str, Any]:
    """Best-effort parse of a function call's JSON-string arguments.

    For display/approval only: a malformed or non-object payload yields ``{}``
    rather than failing the streaming activity (the workflow-side reducer does the
    authoritative parse on its own copy of the stream)."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_token_usage(usage: Any) -> TokenUsage | None:
    """Map OpenAI's ``ResponseUsage`` onto the harness's provider-agnostic
    :class:`TokenUsage`. ``None`` in → ``None`` out; OpenAI reports no separate
    tool-use token count, so that field stays ``None``."""
    if usage is None:
        return None
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        total_tokens=getattr(usage, "total_tokens", None),
        thought_tokens=getattr(output_details, "reasoning_tokens", None),
        cached_tokens=getattr(input_details, "cached_tokens", None),
        tool_use_tokens=None,
    )


class _HarnessStreamToken(BaseModel):
    """The opaque routing token the harness hands the streaming activity.

    Bundles the in-flight turn's :class:`TurnStreamContext` with the requested model id,
    so the observer can open the model-interaction bracket — naming the model — at
    dispatch (``__aenter__``), before any event arrives. Rides the streaming activity
    input as an untyped field, so the factory rehydrates it from a plain dict.
    """

    context: TurnStreamContext
    model: str | None = None


def stream_to_provider(model: str | None) -> _HarnessStreamToken | None:
    """Per-call routing-token provider (wired as ``model_params.stream_to_provider``).

    Called once per streamed request, in workflow context, with the requested model id.
    Resolves the in-flight turn's stream context ambiently off the running workflow
    instance (no explicit runner threading) and bundles it with the model. Returns
    ``None`` outside a harness turn, so the vendored stub falls back to
    ``streaming_topic``.
    """
    context = current_stream_context()
    if context is None:
        return None
    return _HarnessStreamToken(context=context, model=model)


def harness_observer_factory(token: Any) -> StreamObserver[Any]:
    """Turn a streamed call's opaque routing token into a fresh observer.

    The token is the :class:`_HarnessStreamToken` produced by :func:`stream_to_provider`;
    it arrives here rehydrated as a plain dict (it rides the activity input as an untyped
    ``Any`` field), so we validate it back into the model. Wired onto the plugin as
    ``observer_factory=...``.
    """
    tok = (
        token
        if isinstance(token, _HarnessStreamToken)
        else _HarnessStreamToken.model_validate(token)
    )
    return OpenAIStreamObserver(tok.context, model=tok.model)


# ---------------------------------------------------------------------------
# Tool adapter: harness tools -> OpenAI Agents SDK FunctionTools
# ---------------------------------------------------------------------------


def as_openai_agent_tool(
    runner: AgentWorkflowRunner,
    tool_callable: Callable[..., Awaitable[Any]],
    *,
    injections: Mapping[str, Any] | None = None,
    strict_json_schema: bool = True,
) -> "Tool":
    """Adapt a harness tool into an OpenAI Agents SDK ``FunctionTool``.

    ``tool_callable`` must be produced by :func:`harness.agent.tool_defn`,
    :func:`harness.agent.activity_tool_defn`, or :func:`harness.agent.subagent_toolset`.
    The returned OpenAI tool invokes ``runner.run_tool(...)`` for every model tool
    call, so the harness remains responsible for:

    - safe-by-default approval policy evaluation;
    - ``tool_start`` / ``tool_end`` / ``tool_error`` event publication;
    - injected parameters declared with ``Injected[...]``;
    - activity-backed execution for ``@agent.activity_tool_defn`` tools.

    Use ``temporal_agent_harness.ai_sdks.openai_agents.workflow.activity_as_tool``
    directly for plain Temporal activities that do not need harness approval or
    harness tool lifecycle events.
    """
    _require_harness_tool(tool_callable)
    function_schema, function_tool_cls = _agents_tool_symbols()
    schema = function_schema(tool_callable)

    async def on_invoke_tool(_ctx: Any, input: str) -> str:
        try:
            json_data = json.loads(input or "{}")
        except Exception as exc:  # noqa: BLE001 - converted to workflow-visible error
            raise ApplicationError(
                f"Invalid JSON input for tool {schema.name}: {input}",
                type="InvalidToolInput",
                non_retryable=True,
            ) from exc
        if not isinstance(json_data, dict):
            raise ApplicationError(
                f"Tool {schema.name} expected a JSON object input, "
                f"got {type(json_data).__name__}.",
                type="InvalidToolInput",
                non_retryable=True,
            )

        try:
            parsed = schema.params_pydantic_model(**json_data)
            args, kwargs = schema.to_call_args(parsed)
        except Exception as exc:  # noqa: BLE001 - preserve a non-retryable model input error
            raise ApplicationError(
                f"Payload for tool {schema.name!r} does not match its schema: {exc}",
                type="InvalidToolInput",
                non_retryable=True,
            ) from exc

        tool_call_id = getattr(_ctx, "tool_call_id", None)
        if not isinstance(tool_call_id, str) or not tool_call_id:
            tool_call_id = _new_tool_call_id(schema.name)

        try:
            result = await runner.run_tool(
                tool_call_id,
                tool_callable,
                *args,
                injections=injections,
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - model-visible tool failure
            return f"Tool {schema.name!r} failed: {exc}"
        return _stringify_tool_result(result)

    return function_tool_cls(
        name=schema.name,
        description=schema.description or "",
        params_json_schema=schema.params_json_schema,
        on_invoke_tool=on_invoke_tool,
        strict_json_schema=strict_json_schema,
    )


def as_openai_agent_tools(
    runner: AgentWorkflowRunner,
    tool_callables: (
        Mapping[str, Callable[..., Awaitable[Any]]]
        | list[Callable[..., Awaitable[Any]]]
        | tuple[Callable[..., Awaitable[Any]], ...]
    ),
    *,
    injections: Mapping[str, Any] | None = None,
    strict_json_schema: bool = True,
) -> list["Tool"]:
    """Adapt several harness tools into OpenAI Agents SDK tools."""
    tools = (
        list(tool_callables.values())
        if isinstance(tool_callables, Mapping)
        else list(tool_callables)
    )
    return [
        as_openai_agent_tool(
            runner,
            tool,
            injections=injections,
            strict_json_schema=strict_json_schema,
        )
        for tool in tools
    ]


def _require_harness_tool(tool_callable: Callable[..., Any]) -> None:
    if any(getattr(tool_callable, attr, False) for attr in _HARNESS_TOOL_ATTRS):
        return
    name = getattr(tool_callable, "__name__", repr(tool_callable))
    raise TypeError(
        f"{name} is not a harness tool; decorate it with @agent.tool_defn, "
        "@agent.activity_tool_defn, or use agent.subagent_toolset(...)."
    )


def _agents_tool_symbols() -> tuple[Callable[[Callable[..., Any]], Any], type[Any]]:
    function_schema = getattr(
        _import_optional("agents.function_schema"), "function_schema"
    )
    function_tool_cls = getattr(_import_optional("agents.tool"), "FunctionTool")
    return function_schema, function_tool_cls


def _import_optional(module_name: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing == module_name or missing.split(".")[0] in {
            "agents",
            "openai",
            "mcp",
        }:
            raise RuntimeError(_INSTALL_MESSAGE) from exc
        raise


def _new_tool_call_id(tool_name: str) -> str:
    if _in_workflow():
        suffix = workflow.uuid4()
    else:
        suffix = uuid.uuid4()
    return f"openai_agents:{tool_name}:{suffix}"


def _in_workflow() -> bool:
    try:
        return workflow.in_workflow()
    except RuntimeError:
        return False


def _stringify_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, BaseModel):
        return result.model_dump_json()
    if isinstance(result, (dict, list, tuple, int, float, bool)) or result is None:
        return json.dumps(result, default=str)
    return str(result)
