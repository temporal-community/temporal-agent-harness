# ABOUTME: Harness-owned glue for the (unmodified, upstream) Pydantic AI Temporal plugin
# (`pydantic_ai.durable_exec.temporal`). Unlike the vendored OpenAI/Gemini trees — which the
# harness had to modify to add a streaming seam — Pydantic AI already ships a first-class
# `event_stream_handler` hook that runs INSIDE the model-request activity on the live
# `AgentStreamEvent` stream, which is exactly the seam the harness needs. So there is nothing to
# vendor: this module only translates that hook's raw events into the harness turn-stream
# vocabulary (`PydanticAIStreamObserver` / `harness_event_stream_handler`) and adapts harness tools
# onto the SDK (`as_pydantic_ai_tool(s)` / `build_harness_toolset`). Mirrors the structure of
# `openai_agents_harness.py`.

"""Harness integration for the Pydantic AI Temporal plugin.

Wire it onto a worker with the upstream plugin, unmodified:

>>> from pydantic_ai import Agent
>>> from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
>>> from temporal_agent_harness.ai_sdks.pydantic_ai_harness import (
...     HarnessDeps,
...     TemporalAgentHarnessDurability,
...     build_harness_toolset,
... )
>>> toolset = build_harness_toolset([my_tool], id="my-tools")  # runs in-workflow, not in an activity
>>> durable_agent = Agent(
...     "openai:gpt-5.1",  # a model-name string: see TemporalAgentHarnessDurability
...     name="my-agent",
...     deps_type=HarnessDeps,
...     toolsets=[toolset],
...     capabilities=[TemporalAgentHarnessDurability()],
... )
>>> # worker: Worker(..., plugins=[AgentPlugin(durable_agent)]); client: plugins=[PydanticAIPlugin()]

Then, inside the agent workflow, call ``await durable_agent.run(text, deps=deps)`` where
``deps`` carries the in-flight turn's stream context (see :class:`HarnessDeps`); the streamed
model call publishes ``reply_delta`` / ``thought_summary`` / ``tool_requested`` and
``model_interaction_started`` / ``…_ended`` onto the harness turn stream live — the same vocabulary
the Gemini and OpenAI integrations produce.

.. warning::
    Streaming support is experimental and may change in future versions.
"""

from __future__ import annotations

import inspect
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import to_jsonable_python

_INSTALL_MESSAGE = (
    "Pydantic AI support requires the optional `pydantic-ai` extra. "
    "Install it with `uv sync --extra pydantic-ai` or "
    "`pip install 'temporal-agent-harness[pydantic-ai]'`."
)

try:
    from pydantic_ai import FunctionToolset, Tool
    from pydantic_ai.agent.abstract import AbstractAgent
    from pydantic_ai.capabilities import AbstractCapability, ModelIdResolver
    from pydantic_ai.durable_exec.temporal import TemporalDurability
    from pydantic_ai.exceptions import UserError
    from pydantic_ai.messages import (
        BaseToolCallPart,
        ModelMessage,
        PartDeltaEvent,
        PartEndEvent,
        PartStartEvent,
        TextPart,
        TextPartDelta,
        ThinkingPart,
        ThinkingPartDelta,
        tool_return_ta,
    )
    from pydantic_ai.models import (
        KnownModelName,
        Model,
        ModelRequestParameters,
        ModelResolutionContext,
        StreamedResponse,
        infer_model,
    )
    from pydantic_ai.models.wrapper import WrapperModel
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.tools import AgentDepsT, RunContext
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise RuntimeError(_INSTALL_MESSAGE) from exc

from temporal_agent_harness.harness.agent_protocol import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    ThoughtSummaryDelta,
    TokenUsage,
    ToolRequested,
)
from temporal_agent_harness.harness.agent_workflow import (
    AgentWorkflowRunner,
    TurnEventPublisher,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext

if TYPE_CHECKING:
    from pydantic_ai.messages import AgentStreamEvent

__all__ = [
    "HarnessDeps",
    "HarnessStreamingModel",
    "PydanticAIStreamObserver",
    "TemporalAgentHarnessDurability",
    "as_pydantic_ai_tool",
    "as_pydantic_ai_tools",
    "build_harness_toolset",
    "harness_event_stream_handler",
    "harness_model",
]

_HARNESS_TOOL_ATTRS = ("__agent_tool__", "__agent_activity_tool__")


# ---------------------------------------------------------------------------
# Live streaming: raw Pydantic AI stream events -> harness turn vocabulary
# ---------------------------------------------------------------------------
#
# Pydantic AI's `TemporalDurability` capability runs the agent's `event_stream_handler` INSIDE the
# model-request activity, handing it the live model stream (an async iterable of
# `ModelResponseStreamEvent`s: PartStart / PartDelta / PartEnd / FinalResult). That is where this
# observer translates, so — like
# the Gemini `_StreamEventPublisher` — it works in part/delta terms rather than the flat event
# vocabulary the OpenAI Responses stream uses:
#
#   * reply text arrives as a `TextPart`'s initial `content` on its PartStart, then as
#     `TextPartDelta.content_delta` on each PartDelta — publish both straight through as
#     `reply_delta` (they are disjoint fragments, so there is no double count).
#   * `ThinkingPart` / `ThinkingPartDelta` -> `thought_summary` (capability-gated, dumped raw so a
#     consumer can position it).
#   * a custom tool call streams as a `ToolCallPart`: name + id + JSON args accumulate across
#     PartStart/PartDelta, and Pydantic AI synthesizes a PartEnd carrying the COMPLETE part. We emit
#     ONE consolidated `tool_requested` at that PartEnd, keyed by the SDK `tool_call_id` — the same
#     id `as_pydantic_ai_tool` hands to `run_tool`, so `tool_requested` and the eventual
#     `tool_start` / `tool_end` share a `tool_id`.
#
# As with Gemini/OpenAI, `run_tool` OWNS `tool_start` / `tool_end` / `tool_error`; this translator
# only emits `tool_requested`. Hosted/native tool spans are deferred (as in the OpenAI integration).


class PydanticAIStreamObserver:
    """Translate one streamed Pydantic AI model call into harness turn events, live.

    A :class:`StreamObserver` — one instance per streamed call, so its per-call state (pending
    tool-call parts, captured usage) never bleeds across the concurrent calls a shared worker runs.
    Constructed with the call's :class:`TurnStreamContext` and the requested ``model`` id; opens a
    :class:`TurnEventPublisher` on ``__aenter__`` and publishes ``model_interaction_started`` THERE —
    at model-call dispatch, before the first event — so the started→ended span measures the true
    model-call latency (time-to-first-token included). ``on_event`` folds each raw event into harness
    vocabulary; ``__aexit__`` closes the bracket with ``…_ended`` carrying usage recorded via
    :meth:`set_usage`.
    """

    def __init__(self, context: TurnStreamContext, *, model: str | None = None) -> None:
        self._context = context
        # The requested model id, known at dispatch, so BOTH brackets name it. Never read from the
        # stream: doing so would delay `started` past the first event and corrupt the latency.
        self._model = model
        self._stack: AsyncExitStack | None = None
        self._publisher: TurnEventPublisher | None = None
        # Part index -> the tool-call part seen at PartStart, flushed on the matching PartEnd (or,
        # defensively, on close if the stream ended without one).
        self._pending_tool_calls: dict[int, BaseToolCallPart] = {}
        # Token usage for the ended bracket, when the driver can supply it (see set_usage).
        self._usage: TokenUsage | None = None

    async def __aenter__(self) -> Self:
        self._stack = AsyncExitStack()
        self._publisher = await self._stack.enter_async_context(
            AgentWorkflowRunner.publisher_from_activity(self._context)
        )
        # Open the model-interaction span at dispatch, before awaiting any event.
        self._publisher.publish(ModelInteractionStarted(model=self._model))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        try:
            if self._publisher is not None:
                # Flush any tool call whose PartEnd never arrived (defensive — Pydantic AI
                # synthesizes PartEnd for tool-call parts), then always close the started bracket
                # (usage stays None if the stream errored). Published while the publisher is open.
                for part in self._pending_tool_calls.values():
                    self._emit_tool_requested(self._publisher, part)
                self._pending_tool_calls.clear()
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

    def set_usage(self, usage: Any) -> None:
        """Record the call's usage (a Pydantic AI ``RequestUsage``/``RunUsage``) for the ended
        bracket. Called after the stream is drained, before ``__aexit__``."""
        self._usage = _to_token_usage(usage)

    async def on_event(self, event: AgentStreamEvent) -> None:
        """Fold one raw model-stream event into harness vocabulary."""
        pub = self._publisher
        if pub is None:
            return

        if isinstance(event, PartStartEvent):
            part = event.part
            if isinstance(part, TextPart):
                if part.content:
                    pub.publish(ReplyDelta(text=part.content))
            elif isinstance(part, ThinkingPart):
                if part.content:
                    pub.publish(ThoughtSummaryDelta(delta=_dump(part)))
            elif isinstance(part, BaseToolCallPart):
                # A tool call is opening; its args may still stream. Hold it and publish one
                # consolidated tool_requested on the part's PartEnd.
                self._pending_tool_calls[event.index] = part
        elif isinstance(event, PartDeltaEvent):
            delta = event.delta
            if isinstance(delta, TextPartDelta):
                if delta.content_delta:
                    pub.publish(ReplyDelta(text=delta.content_delta))
            elif isinstance(delta, ThinkingPartDelta) and (
                delta.content_delta or delta.signature_delta
            ):
                pub.publish(ThoughtSummaryDelta(delta=_dump(delta)))
            # A ToolCallPartDelta streams arg fragments; the COMPLETE part arrives on PartEnd, so
            # there is nothing to consolidate here.
        elif isinstance(event, PartEndEvent):
            if isinstance(event.part, BaseToolCallPart):
                self._pending_tool_calls.pop(event.index, None)
                self._emit_tool_requested(pub, event.part)

    def _emit_tool_requested(
        self, pub: TurnEventPublisher, part: BaseToolCallPart
    ) -> None:
        """Publish the consolidated ``tool_requested`` for one custom tool call.

        ``tool_id`` is the SDK ``tool_call_id`` (shared with the execution lifecycle in
        ``run_tool`` via ``RunContext.tool_call_id``). ``args_as_dict`` is best-effort for
        display/approval; the workflow-side reducer does the authoritative parse on its own copy."""
        pub.publish(
            ToolRequested(
                tool_id=part.tool_call_id,
                tool_name=part.tool_name,
                tool_input=_args_as_dict(part),
            )
        )


def _dump(obj: Any) -> dict[str, Any]:
    """Dump a Pydantic AI message part/delta (a dataclass) to a JSON-safe dict."""
    dumped = to_jsonable_python(obj)
    return dumped if isinstance(dumped, dict) else {"value": dumped}


def _args_as_dict(part: BaseToolCallPart) -> dict[str, Any]:
    """Best-effort dict of a tool call's arguments for display/approval (never raises)."""
    try:
        args = part.args_as_dict()
    except Exception:  # noqa: BLE001 - display-only; a malformed payload must not break streaming
        return {}
    return args if isinstance(args, dict) else {}


def _to_token_usage(usage: Any) -> TokenUsage | None:
    """Map Pydantic AI's ``RequestUsage``/``RunUsage`` onto the harness's provider-agnostic
    :class:`TokenUsage`. ``None`` in → ``None`` out. Reasoning tokens are not a first-class field
    upstream — they ride in ``details['reasoning_tokens']`` — and there is no separate tool-use
    count, so that stays ``None``."""
    if usage is None:
        return None
    details = getattr(usage, "details", None) or {}
    total = getattr(usage, "total_tokens", None)
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        thought_tokens=details.get("reasoning_tokens"),
        cached_tokens=getattr(usage, "cache_read_tokens", None) or None,
        tool_use_tokens=None,
        total_tokens=total,
    )


# ---------------------------------------------------------------------------
# The event_stream_handler wired onto the TemporalDurability capability
# ---------------------------------------------------------------------------


class HarnessDeps(BaseModel):
    """A ready ``deps`` type (or base to subclass) carrying the harness per-run handles.

    Build it per turn from just the runner and pass it to ``agent.run(...)``::

        await temporal_agent.run(message.text, deps=HarnessDeps(runner=self._runner))

    You pass only ``runner``; the initializer snapshots ``harness_stream_context`` from
    ``runner.current_stream_context`` for you. Both are needed because they are consumed in different
    places: the ``runner`` stays a live in-workflow reference the harness tools read off ``ctx.deps``,
    while ``harness_stream_context`` must be resolved workflow-side and carried across into the model
    activity — that is where the streaming handler runs, and the (non-serializable) runner cannot
    follow it there. So the snapshot has to happen here, on the workflow side.

    ``runner`` is ``Field(exclude=True)``: it is dropped when ``deps`` is serialized into the model
    activity, so a non-serializable runtime object never crosses the activity boundary (the handler
    there only needs the already-snapshotted context). Subclass to add your own dependencies; the
    harness only reads these two attributes. Pass ``harness_stream_context`` explicitly to override
    the snapshot (e.g. in tests).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    runner: AgentWorkflowRunner | None = Field(default=None, exclude=True, repr=False)
    harness_stream_context: TurnStreamContext | None = None

    @model_validator(mode="after")
    def _snapshot_stream_context(self) -> HarnessDeps:
        # Convenience: given just a runner, capture the in-flight turn's stream context here, on the
        # workflow side, where the runner is live. Skipped when the caller already supplied a context
        # (explicit override), and a no-op on deserialization inside the activity (runner is dropped
        # by exclude=True, so it is None there while harness_stream_context is already populated).
        if self.runner is not None and self.harness_stream_context is None:
            self.harness_stream_context = self.runner.current_stream_context
        return self


def _resolve_stream_context(ctx: RunContext[Any]) -> TurnStreamContext | None:
    """Read the turn stream context off ``ctx.deps`` (``None`` when absent / not a harness turn).

    Robust to the deps arriving either fully typed (a real :class:`TurnStreamContext`, when the
    agent's ``deps_type`` declares it) or as a plain dict (an untyped payload)."""
    deps = getattr(ctx, "deps", None)
    raw = getattr(deps, "harness_stream_context", None) if deps is not None else None
    if raw is None:
        return None
    if isinstance(raw, TurnStreamContext):
        return raw
    return TurnStreamContext.model_validate(raw)


async def harness_event_stream_handler(
    ctx: RunContext[Any], stream: AsyncIterable[AgentStreamEvent]
) -> None:
    """Pydantic AI ``event_stream_handler`` that streams model events onto the harness turn.

    :class:`TemporalAgentHarnessDurability` installs it. Pydantic AI invokes it in two places, both of
    which this handler must fully drain:

    * inside the ``…__model_request_stream`` activity, on the live model stream. There
      :class:`HarnessStreamingModel` has already opened this call's observer, and the handler feeds
      it each event as it arrives.
    * once per ``…__event_stream_handler`` activity for each in-workflow ``HandleResponseEvent``
      (tool call/result). No observer is open there, and the harness owns tool lifecycle via
      ``run_tool``, so this handler drains those — one lightweight activity per response event,
      inherent to setting a handler.
    """
    observer = _ACTIVE_OBSERVER.get()
    if observer is None:
        async for _ in stream:
            pass
        return
    async for event in stream:
        await observer.on_event(event)


# The observer of the model call running in this activity, set by HarnessStreamingModel while its
# stream is open. The activity enters the model's stream and then calls the event-stream handler in
# the same task, so the handler reads the observer its own call opened, never another activity's.
_ACTIVE_OBSERVER: ContextVar[PydanticAIStreamObserver | None] = ContextVar(
    "pydantic_ai_harness_active_observer", default=None
)


class HarnessStreamingModel(WrapperModel):
    """Wraps the agent's model so each streamed call publishes its harness model-interaction bracket.

    Its ``request_stream`` runs inside the model-request activity, where it sees the provider's
    ``StreamedResponse``. It opens a :class:`PydanticAIStreamObserver` before the request is sent,
    so ``model_interaction_started`` measures time-to-first-token. It closes the observer once the
    stream is drained, with the call's usage on ``model_interaction_ended``. The deltas in between
    reach the observer through :func:`harness_event_stream_handler`.

    Outside a harness turn (no stream context on ``deps``) it is a plain pass-through.
    """

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        context = _resolve_stream_context(run_context) if run_context is not None else None
        if context is None:
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as streamed:
                yield streamed
            return

        # The observer is entered first, so `started` is published before the request is sent.
        async with (
            PydanticAIStreamObserver(context, model=self.model_name) as observer,
            self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as streamed,
        ):
            token = _ACTIVE_OBSERVER.set(observer)
            try:
                yield streamed
            finally:
                _ACTIVE_OBSERVER.reset(token)
            observer.set_usage(streamed.usage)


@dataclass(init=False, kw_only=True)
class TemporalAgentHarnessDurability(TemporalDurability[AgentDepsT]):
    """Pydantic AI's ``TemporalDurability`` capability with harness turn streaming wired in.

    Use it in place of ``TemporalDurability``; it takes the same arguments except
    ``event_stream_handler``, which it sets to :func:`harness_event_stream_handler`::

        Agent("openai:gpt-5.1", name="my-agent", deps_type=HarnessDeps, toolsets=[toolset],
              capabilities=[TemporalAgentHarnessDurability()])

    It also wraps each model it resolves in a :class:`HarnessStreamingModel`. Inside the
    model-request activity the worker rebuilds the agent's model from its model-name string through
    ``resolve_model_id``, so the wrapper is what the activity streams from.

    A ``Model`` instance given as the agent's model is used as-is in the activity, never resolved,
    so it has to arrive already wrapped: pass ``harness_model(instance)``. Binding rejects an
    unwrapped instance, which would otherwise run with no turn streaming at all.

    To build models from their names yourself (a custom provider, or per-tenant credentials read
    off ``deps``), pass the resolver here as ``model_resolver``. It takes what Pydantic AI's
    ``ResolveModelId`` takes and is consulted first; returning ``None`` falls back to the
    ``models=`` registry, then ``infer_model``. Whatever it returns is wrapped like any other model.
    The harness owns model-name resolution, so binding rejects any other capability on the agent
    that resolves model names (such as a separate ``ResolveModelId``).

    The resolver runs in workflow code as well as in the activity: once when ``agent.run()`` picks
    the run's model (again on replay), then inside each model-request activity. So it must be
    deterministic and do no I/O. Fetch a credential only when ``activity.in_activity()`` is true,
    and don't put secrets on ``deps``: deps are serialized into every activity input, which lands
    in workflow history. Carry an identifier there (a tenant id) and look the secret up in the
    activity instead.
    """

    def __init__(
        self, *, model_resolver: ModelIdResolver[AgentDepsT] | None = None, **kwargs: Any
    ) -> None:
        super().__init__(event_stream_handler=harness_event_stream_handler, **kwargs)
        self._model_resolver = model_resolver

    def for_agent(self, agent: AbstractAgent[AgentDepsT, Any]) -> AbstractCapability[AgentDepsT]:
        model = agent.model
        if isinstance(model, Model) and not _wraps_harness_model(model):
            raise UserError(
                f"Agent {agent.name!r} has a {type(model).__name__} instance as its model, which "
                "TemporalAgentHarnessDurability cannot stream from. Wrap it: "
                "Agent(harness_model(<model>), ...), or pass a model-name string such as "
                "'openai:gpt-5.1'."
            )
        # Durability capabilities bind last, so every other capability is already in the tree.
        resolvers: list[AbstractCapability[AgentDepsT]] = []
        agent.root_capability.apply(
            lambda cap: resolvers.append(cap)
            if cap is not self and cap.has_resolve_model_id
            else None
        )
        if resolvers:
            names = ", ".join(type(cap).__name__ for cap in resolvers)
            raise UserError(
                f"Agent {agent.name!r} has another capability that resolves model names ({names}). "
                "The Temporal agent harness owns model-name resolution; pass the resolver as "
                "TemporalAgentHarnessDurability(model_resolver=...) instead."
            )
        return super().for_agent(agent)

    async def resolve_model_id(
        self,
        ctx: ModelResolutionContext[AgentDepsT],
        *,
        model_id: KnownModelName | str,
    ) -> Model | None:
        model = await self._resolve_with_author_resolver(ctx, model_id)
        if model is None:
            model = await super().resolve_model_id(ctx, model_id=model_id) or infer_model(model_id)
        return model if _wraps_harness_model(model) else HarnessStreamingModel(model)

    async def _resolve_with_author_resolver(
        self, ctx: ModelResolutionContext[AgentDepsT], model_id: str
    ) -> Model | None:
        if self._model_resolver is None:
            return None
        resolved = self._model_resolver(ctx, model_id)
        return await resolved if inspect.isawaitable(resolved) else resolved


def harness_model(model: Model) -> HarnessStreamingModel:
    """Wrap a ``Model`` instance so a :class:`TemporalAgentHarnessDurability` agent streams from it::

        Agent(harness_model(OpenAIChatModel("gpt-5.1", provider=...)), name="my-agent", ...,
              capabilities=[TemporalAgentHarnessDurability()])

    A model-name string needs no wrapping; the capability wraps what it resolves."""
    return model if isinstance(model, HarnessStreamingModel) else HarnessStreamingModel(model)


def _wraps_harness_model(model: Model) -> bool:
    """Whether ``model`` is, or wraps somewhere inside it, a :class:`HarnessStreamingModel`."""
    current: Model | None = model
    while current is not None:
        if isinstance(current, HarnessStreamingModel):
            return True
        current = current.wrapped if isinstance(current, WrapperModel) else None
    return False


# ---------------------------------------------------------------------------
# Tool adapter: harness tools -> Pydantic AI Tools (executed in-workflow via run_tool)
# ---------------------------------------------------------------------------


def as_pydantic_ai_tool(
    tool_callable: Callable[..., Awaitable[Any]],
    *,
    injections: Mapping[str, Any] | None = None,
) -> Tool[Any]:
    """Adapt a harness tool into a Pydantic AI :class:`~pydantic_ai.Tool`.

    ``tool_callable`` must be produced by :func:`harness.agent.tool_defn`,
    :func:`harness.agent.activity_tool_defn`, or :func:`harness.agent.subagent_toolset`. The
    returned tool invokes ``runner.run_tool(...)`` for every model tool call, so the harness remains
    responsible for approval-policy evaluation, ``tool_start`` / ``tool_end`` / ``tool_error``
    events, ``Injected[...]`` parameters, and activity-backed execution.

    The runner is resolved per call from ``ctx.deps`` (a :class:`HarnessDeps`) — threaded
    EXPLICITLY by the author at the ``agent.run(deps=...)`` call site, not assumed off the workflow
    instance. This is what lets one module-level agent serve every concurrent workflow
    correctly: each run supplies its own runner via its own ``deps``.

    The tool MUST run in-workflow (the harness approval gate and event publishing are workflow-side),
    so register it with the Temporal activity wrapper DISABLED — use :func:`build_harness_toolset`,
    which marks its tools ``metadata={"temporal": False}``. (When wrongly run
    inside an activity, ``ctx.deps`` has no live ``runner`` and the call raises with guidance.)
    """
    _require_harness_tool(tool_callable)
    name, description, json_schema = _tool_schema(tool_callable)

    async def on_invoke_tool(ctx: RunContext[Any], **kwargs: Any) -> Any:
        runner = _runner_from_ctx(ctx, tool_name=name)
        try:
            result = await runner.run_tool(
                ctx.tool_call_id,
                tool_callable,
                injections=injections,
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - surface as a model-visible tool failure
            # Mirror the OpenAI adapter: a denied/failed tool becomes a result the model sees and
            # moves past, rather than crashing the turn.
            result = f"Tool {name!r} failed: {exc}"
        steering = runner.claim_steering(tool_id=ctx.tool_call_id)
        if steering is None:
            return result
        return steering.append_to(_model_response_str(result))

    return Tool.from_schema(
        on_invoke_tool,
        name=name,
        description=description,
        json_schema=json_schema,
        takes_ctx=True,
    )


def _model_response_str(result: Any) -> str:
    """``result`` as the text Pydantic AI would send the model for it, so appending steering
    changes nothing but the appended block (``ToolReturnPart.model_response_str``)."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    return tool_return_ta.dump_json(result, by_alias=True).decode()


def as_pydantic_ai_tools(
    tool_callables: (
        Mapping[str, Callable[..., Awaitable[Any]]]
        | Sequence[Callable[..., Awaitable[Any]]]
    ),
    *,
    injections: Mapping[str, Any] | None = None,
) -> list[Tool[Any]]:
    """Adapt several harness tools into Pydantic AI tools."""
    tools = (
        list(tool_callables.values())
        if isinstance(tool_callables, Mapping)
        else list(tool_callables)
    )
    return [as_pydantic_ai_tool(tool, injections=injections) for tool in tools]


def build_harness_toolset(
    tool_callables: (
        Mapping[str, Callable[..., Awaitable[Any]]]
        | Sequence[Callable[..., Awaitable[Any]]]
    ),
    *,
    id: str = "harness_tools",
    injections: Mapping[str, Any] | None = None,
) -> FunctionToolset[Any]:
    """Build a Pydantic AI ``FunctionToolset`` of harness tools that run in-workflow.

    Build it once (module level is fine — no runner is captured here; each run supplies its runner
    via ``deps``) and pass it to the agent::

        toolset = build_harness_toolset([get_weather], id="hello-tools")
        agent = Agent("openai:gpt-5.1", toolsets=[toolset], capabilities=[TemporalAgentHarnessDurability()])

    Every tool carries ``metadata={"temporal": False}``, which tells ``TemporalDurability`` not to
    wrap its calls in an activity. The harness needs them in-workflow for its approval gate
    (``workflow.wait_condition``), its tool lifecycle events, and reading the live runner off
    ``deps``; none of that works inside an activity.
    """
    tools = as_pydantic_ai_tools(tool_callables, injections=injections)
    return FunctionToolset(tools, id=id, metadata={"temporal": False})


def _tool_schema(
    tool_callable: Callable[..., Any],
) -> tuple[str, str, dict[str, Any]]:
    """Derive ``(name, description, params_json_schema)`` for a harness tool.

    The harness tool decorators stamp the returned object with a MODEL-FACING ``__signature__`` /
    ``__annotations__`` (``self`` and ``Injected[...]`` parameters already stripped), so letting
    Pydantic AI introspect it directly yields exactly the schema the model should see — the same
    approach the Gemini ``function_param`` helper uses with Gemini's own introspector. We build a
    throwaway ``Tool`` (never executed) purely to reuse Pydantic AI's schema generation, then hand
    the schema to :meth:`Tool.from_schema` alongside our own run_tool-backed callable."""
    probe = Tool(tool_callable, takes_ctx=False)
    schema = probe.function_schema
    name = getattr(tool_callable, "__name__", probe.name)
    description = schema.description or (tool_callable.__doc__ or "").strip()
    return name, description, schema.json_schema


def _require_harness_tool(tool_callable: Callable[..., Any]) -> None:
    if any(getattr(tool_callable, attr, False) for attr in _HARNESS_TOOL_ATTRS):
        return
    name = getattr(tool_callable, "__name__", repr(tool_callable))
    raise TypeError(
        f"{name} is not a harness tool; decorate it with @agent.tool_defn, "
        "@agent.activity_tool_defn, or use agent.subagent_toolset(...)."
    )


def _runner_from_ctx(ctx: RunContext[Any], *, tool_name: str) -> AgentWorkflowRunner:
    """Read the live :class:`AgentWorkflowRunner` the author threaded onto ``ctx.deps``.

    This is the explicit-threading contract (see :class:`HarnessDeps`): the runner is NOT assumed off
    ``workflow.instance()`` — it is whatever the author passed as ``agent.run(deps=HarnessDeps(
    runner=...))``. A harness tool runs in-workflow, where ``deps`` is still that live object, so the
    runner is available here. If it is missing, the tool was most likely run inside a Temporal
    activity (its ``temporal`` metadata did not disable the wrapper) or ``deps`` did not carry the
    runner; raise with guidance."""
    runner = getattr(getattr(ctx, "deps", None), "runner", None)
    if isinstance(runner, AgentWorkflowRunner):
        return runner
    raise RuntimeError(
        f"harness Pydantic AI tool {tool_name!r} has no live AgentWorkflowRunner on its "
        "RunContext.deps. Pass deps=HarnessDeps(runner=<your runner>) to agent.run(...), and "
        "make sure the tool runs in-workflow (use build_harness_toolset(...) so its Temporal "
        "activity wrapper is disabled)."
    )
