"""Worker-side activity for the Interactions API.

``gemini_interactions_create_streamed`` is the Interactions-API counterpart
of :func:`gemini_api_client_async_request_streamed` in
:mod:`google_genai_plugin._gemini_activity`. It holds the real
``genai.Client``, calls ``client.aio.interactions.create(**kwargs)``,
publishes ``reply_delta`` events for streaming text content (when a
``stream_context`` was forwarded by the shim), and returns the full,
unredacted sequence of SSE events.

The contract is: the activity returns events in the same order and shape
the SDK would have yielded them, so workflow-side code can consume the
result via the :class:`TemporalAsyncInteractions` shim with the same
``async for event in stream`` pattern it would use against the SDK
directly.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from google.genai import Client as GeminiClient
from google.genai._interactions import AsyncStream
from google.genai._interactions.types import (
    FileSearchCallStep,
    FileSearchResultStep,
    FunctionCallStep,
    InteractionCompletedEvent,
    InteractionSSEEvent,
    StepDelta,
    StepStart,
    StepStop,
)
from google.genai._interactions.types.usage import Usage
from google.genai._interactions.types.step_delta import (
    DeltaArgumentsDelta,
    DeltaFileSearchCall,
    DeltaFileSearchResult,
    DeltaText,
    DeltaTextAnnotationDelta,
    DeltaThoughtSummary,
)
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
    AgentWorkflowRunner,
    TurnEventPublisher,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext
from temporalio import activity

from ._interactions_models import _InteractionResult


def make_gemini_interactions_create_streamed(client: GeminiClient):
    """Build the activity that calls ``interactions.create`` against ``client``."""

    @activity.defn
    async def gemini_interactions_create_streamed(
        kwargs: dict[str, Any],
        stream_context: TurnStreamContext | None,
    ) -> _InteractionResult:
        """Run one streamed ``interactions.create`` call.

        ``kwargs`` is forwarded straight into the SDK — the shim is
        responsible for building exactly the dict it wants
        ``client.aio.interactions.create`` to receive. Iterates the SSE
        stream, collects every event verbatim (serialized as a JSON-safe
        dict), and republishes streaming text content as ``reply_delta``
        events on the parent workflow's stream when a ``stream_context``
        was provided.
        """
        collected: list[dict[str, Any]] = []

        async with AsyncExitStack() as stack:
            publisher: TurnEventPublisher | None = None
            event_publisher: _StreamEventPublisher | None = None
            if stream_context is not None:
                publisher = await stack.enter_async_context(
                    AgentWorkflowRunner.publisher_from_activity(stream_context)
                )
                event_publisher = _StreamEventPublisher(publisher)

            # Bracket the whole streaming call as ONE model interaction: the model
            # is doing work between these two events, distinct from the agent's
            # tool runs / waiting that happen outside this activity. The pair is
            # published around the stream so a turn's several model calls each show
            # up as their own started→ended span. The ended is in a finally so it
            # always closes the started — even if the stream errors — while the
            # publisher context is still open. Both events carry the requested model; the
            # ended also carries token usage read off the terminal interaction.completed
            # event (so usage stays None if the stream errors before completing).
            model = kwargs.get("model")
            usage: TokenUsage | None = None
            if publisher is not None:
                publisher.publish(ModelInteractionStarted(model=model))
            try:
                stream: AsyncStream[
                    InteractionSSEEvent
                ] = await client.aio.interactions.create(**kwargs)
                async for event in stream:
                    if event_publisher is not None:
                        event_publisher.handle(event)
                    if isinstance(event, InteractionCompletedEvent):
                        usage = _to_token_usage(event.interaction.usage)
                    collected.append(event.model_dump(exclude_none=True, mode="json"))
            finally:
                if publisher is not None:
                    publisher.publish(ModelInteractionEnded(model=model, usage=usage))

        return _InteractionResult(events=collected)

    return gemini_interactions_create_streamed


def _to_token_usage(usage: Usage | None) -> TokenUsage | None:
    """Map Gemini's ``Usage`` onto the harness's provider-agnostic :class:`TokenUsage`.

    Carries the cross-provider totals the harness vocabulary defines; Gemini's
    per-modality breakdowns and grounding-tool counts are intentionally dropped (the
    full, unredacted usage is still on the interaction.completed event in the returned
    result for anything that needs it). ``None`` in → ``None`` out."""
    if usage is None:
        return None
    return TokenUsage(
        input_tokens=usage.total_input_tokens,
        output_tokens=usage.total_output_tokens,
        thought_tokens=usage.total_thought_tokens,
        cached_tokens=usage.total_cached_tokens,
        tool_use_tokens=usage.total_tool_use_tokens,
        total_tokens=usage.total_tokens,
    )


@dataclass(frozen=True)
class _PendingToolStart:
    """A built-in tool's call step, recorded at its step.start and awaiting that
    step's step.stop to publish a consolidated ``tool_start``. ``tool_id`` is the
    call step's own ``id``."""

    tool_id: str
    tool_name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True)
class _PendingToolEnd:
    """A built-in tool's result step, recorded at its step.start and awaiting that
    step's step.stop to publish ``tool_end``. ``tool_id`` is the result step's
    ``call_id`` (equal to its call step's ``id``), so ``tool_start`` and
    ``tool_end`` of one invocation share a tool_id."""

    tool_id: str
    tool_name: str


# ---------------------------------------------------------------------------
# Step vs. delta = container vs. contents
#
# A step is one unit of work the model does in a turn — emit output text, run a
# thought, call file_search, return file_search results. Each step has a
# three-event lifecycle:
#
#   step.start  (StepStart)  ── "a step of kind X is beginning"   ← carries a `step: Step`
#   step.delta  (StepDelta)  ── "here's more content for it"      ← carries a `delta: Delta`   (repeats N times)
#   step.delta  (StepDelta)  ── "...and more"
#      ...
#   step.stop   (StepStop)   ── "that step is done"               ← carries nothing but `index`
#
# The trap: `Step` (on step.start) and `Delta` (on step.delta) are two SEPARATE
# discriminated unions with near-identical member names. As a rule a step's
# *content* arrives on its step.delta, not on the step.start that merely opens
# it (e.g. reply text streams as DeltaText, never on the ModelOutputStep that
# opens it) — so match the delta, not just the start, or you silently drop the
# payload. file_search is the exception that proves the rule: confirmed via live
# event logs, BOTH its call/result steps AND deltas are empty {type, signature}
# markers — the backend never sends queries or result chunks through them. Its
# retrieved content surfaces as FileCitation `text_annotation` deltas instead.
# ---------------------------------------------------------------------------
class _StreamEventPublisher:
    """Republishes interaction-stream events as workflow turn events.

    Stateful across one stream because both a built-in tool call and a custom
    function call span multiple events. We consolidate them into the tool vocabulary
    the custom-tool path (``AgentWorkflowRunner.run_tool``) already speaks:

      * ``tool_requested`` — published ONCE for each CUSTOM function call, when its
        ``FunctionCallStep`` *stops* (so streamed ``DeltaArgumentsDelta`` fragments
        are consolidated into the full args). This marks the moment the model first
        requested the tool, as it streams out of the LLM — distinct from it actually
        executing. The workflow's ``run_tool`` still owns the EXECUTION lifecycle
        (``tool_start`` / ``tool_end`` / ``tool_error``) for these calls; we do NOT
        publish those here.
      * ``tool_start`` — published ONCE for each BUILT-IN (server-side) tool, when
        its ``*CallStep`` *stops*. Built-in tools run inside Gemini's backend with no
        request/dispatch gap the harness controls, so there is no separate
        ``tool_requested`` for them — the call IS the execution. Deferring to the
        stop lets a call whose arguments stream over ``DeltaArgumentsDelta`` be
        consolidated. (file_search takes no args, so its tool_start carries an empty
        input.)
      * ``tool_end`` — published ONCE for each BUILT-IN tool, when its ``*ResultStep``
        *stops*, carrying the final payload. A built-in tool whose result Google
        happens to transmit as deltas (e.g. code_execution) is still a SINGLE logical
        output: those deltas would be accumulated into this one payload, NOT streamed
        as ``tool_progress_delta``. (file_search's result is empty.)

    ``tool_progress_delta`` is reserved for tools doing genuine multi-step work
    that report intermediate progress (e.g. a future custom tool that connects to
    a service, issues several requests, then returns). Whether a tool streams
    progress is a property of the TOOL's semantics, not of how a transport chunks
    bytes — so no built-in Gemini tool produces it here. Such a producer would
    publish from its own activity via ``AgentWorkflowRunner.publisher_from_activity``
    (the mechanism this activity uses for ``reply_delta``).

    ``StepStop`` carries only an ``index`` (no step kind), so we record
    ``index -> …`` when each call/result step *starts*, letting the otherwise
    anonymous stop be matched back to the tool (and phase) it closes.

    The ``InteractionSSEEvent`` union is discriminated by ``event_type`` (see
    ``PropertyInfo(discriminator="event_type")`` on the type alias); ``handle``
    matches that, then each ``_on_*`` matches its own sub-union (the ``Step``
    variant for ``step.start``, the ``Delta`` variant for ``step.delta``).
    """

    def __init__(self, publisher: TurnEventPublisher) -> None:
        self._publisher = publisher
        # Call-step index -> the pending tool_start, published on that step's stop
        # (built-in tools only). See :class:`_PendingToolStart`.
        self._pending_tool_starts: dict[int, _PendingToolStart] = {}
        # Result-step index -> the pending tool_end, published on that step's stop
        # (built-in tools only). See :class:`_PendingToolEnd`.
        self._pending_tool_ends: dict[int, _PendingToolEnd] = {}
        # Function-call-step index -> the FunctionCallStep, awaiting that step's
        # step.stop to publish one consolidated tool_requested (custom tools).
        self._pending_tool_requests: dict[int, FunctionCallStep] = {}
        # Function-call-step index -> accumulated JSON-string argument fragments
        # streamed on DeltaArgumentsDelta, parsed once the step stops.
        self._tool_request_arg_buffers: dict[int, str] = {}

    def handle(self, event: InteractionSSEEvent) -> None:
        """Dispatch one event on the union's ``event_type`` discriminator."""
        match event:
            case StepStart():
                self._on_step_start(event)
            case StepDelta():
                self._on_step_delta(event)
            case StepStop():
                self._on_step_stop(event)

            # The remaining union members are NOT step-lifecycle events:
            # InteractionCreatedEvent ("interaction.created"),
            # InteractionCompletedEvent ("interaction.completed"),
            # InteractionStatusUpdate ("interaction.status_update") and
            # ErrorEvent ("error"). Skipped for now, but we WILL need at least
            # some — `error` should surface to the UI, and `interaction.completed`
            # carries final state/usage. TODO: give these real handling.
            case _:
                pass

    def _on_step_start(self, event: StepStart) -> None:
        """A step is opening (``event.step`` is the ``Step`` sub-union).

        Built-in tool call/result steps are recorded here and published on their
        respective ``step.stop`` (see ``_on_step_stop``); to add another built-in
        tool, record its call step and result step alongside file_search's.

        Custom function calls (``FunctionCallStep``) are recorded here too, but only
        to publish a ``tool_requested`` on their ``step.stop`` — the model's request,
        carrying name + consolidated args. Their EXECUTION lifecycle
        (tool_start/tool_end/tool_error) is owned by ``AgentWorkflowRunner.run_tool``
        on the workflow side, so we deliberately do not emit those here.
        ``FunctionResultStep`` is likewise skipped (the result comes back through
        run_tool, not the model stream).
        """
        match event.step:
            case FileSearchCallStep() as call:
                # file_search exposes no arguments; another built-in tool would
                # capture them here (and/or accumulate DeltaArgumentsDelta) so the
                # deferred tool_start carries the full args.
                self._pending_tool_starts[event.index] = _PendingToolStart(
                    tool_id=call.id, tool_name="file_search", tool_input={}
                )
            case FileSearchResultStep() as result:
                self._pending_tool_ends[event.index] = _PendingToolEnd(
                    tool_id=result.call_id, tool_name="file_search"
                )
            case FunctionCallStep() as call:
                # The model is streaming out a custom function-call request. Record
                # it; its full args may still arrive as DeltaArgumentsDelta, so we
                # publish the consolidated tool_requested on this step's stop.
                self._pending_tool_requests[event.index] = call

            # Other step kinds — FunctionResultStep (see docstring) and content
            # steps like ModelOutputStep/ThoughtStep (content arrives on
            # step.delta). Skip.
            case _:
                pass

    def _on_step_delta(self, event: StepDelta) -> None:
        """Incremental CONTENT for the open step (``event.delta`` sub-union)."""
        match event.delta:
            case DeltaText(text=text) if text:
                self._publisher.publish(ReplyDelta(text=text))
            case DeltaThoughtSummary() as delta:
                self._publisher.publish(
                    ThoughtSummaryDelta(
                        delta=delta.model_dump(exclude_none=True, mode="json")
                    )
                )
            case DeltaTextAnnotationDelta() as delta:
                # FileCitation.start_index / end_index are UTF-8 BYTE offsets
                # into the assembled reply text (per the SDK docstring), not
                # character indices. To slice them onto the rendered text,
                # concatenate the reply_delta chunks, ``.encode("utf-8")``,
                # then slice ``[start_index:end_index]``. Indexing the Python
                # string directly only works for pure-ASCII output.
                self._publisher.publish(
                    TextAnnotationDelta(
                        delta=delta.model_dump(exclude_none=True, mode="json")
                    )
                )
            case DeltaArgumentsDelta(arguments=args) if args:
                # A custom function call's arguments stream as JSON-string
                # fragments; buffer them per step index to consolidate into the
                # tool_requested published when the call step stops.
                self._tool_request_arg_buffers[event.index] = (
                    self._tool_request_arg_buffers.get(event.index, "") + args
                )
            # file_search's call/result deltas are empty {type, signature}
            # markers (confirmed via live logs). For future built-in tools:
            #   * call-arg deltas (DeltaArgumentsDelta) would be accumulated and
            #     folded into the single tool_start published on the call stop;
            #   * content-bearing result deltas (e.g. DeltaCodeExecutionResult)
            #     would be accumulated and folded into the single tool_end payload
            #     — NOT streamed as tool_progress_delta. Google chunking one logical
            #     output into deltas is a transport detail, not the genuine
            #     multi-step progress that tool_progress_delta is for.
            # Nothing to collect for file_search, so skip.
            case DeltaFileSearchCall() | DeltaFileSearchResult():
                pass

            # Other delta kinds (DeltaImage, DeltaCodeExecutionCall, …) belong to
            # tools/modalities this agent doesn't use. Skip.
            case _:
                pass

    def _on_step_stop(self, event: StepStop) -> None:
        """A step is complete. ``StepStop`` carries only ``index``.

        Publish the consolidated ``tool_requested`` (if a custom function-call step
        closed), ``tool_start`` (if a built-in call step closed) or ``tool_end`` (if
        a built-in result step closed). An index belongs to exactly one step, so it
        matches at most one pending map. Any other stop closes a step we don't
        bracket (text/thought/function-result steps) and is ignored.
        """
        start = self._pending_tool_starts.pop(event.index, None)
        if start is not None:
            self._publisher.publish(
                ToolStartEvent(
                    tool_id=start.tool_id,
                    tool_name=start.tool_name,
                    tool_input=start.tool_input,
                )
            )
            return
        end = self._pending_tool_ends.pop(event.index, None)
        if end is not None:
            self._publisher.publish(
                ToolEndEvent(tool_id=end.tool_id, tool_name=end.tool_name)
            )
            return
        call = self._pending_tool_requests.pop(event.index, None)
        if call is not None:
            self._publisher.publish(
                ToolRequested(
                    tool_id=call.id,
                    tool_name=call.name,
                    tool_input=self._consolidated_args(event.index, call),
                )
            )

    def _consolidated_args(
        self, index: int, call: FunctionCallStep
    ) -> dict[str, Any]:
        """Resolve a function call's args: prefer the buffered JSON-string deltas,
        else fall back to any args inlined on the ``FunctionCallStep`` itself.

        Best-effort for display/approval: a malformed buffer yields ``{}`` rather
        than failing the activity (the workflow-side reducer does the authoritative
        parse on its own copy of the stream).
        """
        raw = self._tool_request_arg_buffers.pop(index, "")
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return dict(call.arguments) if call.arguments else {}
