# ABOUTME: The harness's OpenTelemetry seam — semantic ``gen_ai`` spans for the agent
# lifecycle (turn, model interaction, tool call, approval gate), layered on Temporal's
# replay-safe OTel integration.
#
# This module exists because the AgentEvent stream and an eval/observability backend want
# DIFFERENT things. The stream is the harness's durable, offset-addressed record of what an
# agent did (see ``agent_protocol/events.py``); a backend like Langfuse wants a *trace* — a
# span tree with durations, parent/child nesting across process boundaries, and ``gen_ai.*``
# attributes it can turn into token/cost accounting. These spans are that second view. The two
# are joined by :class:`~temporal_agent_harness.harness.agent_protocol.events.TurnStarted`,
# which carries the turn span's ``otel_trace_id`` so any out-of-band consumer (a scorer, a batch
# eval runner, a UI deep-link) can reference the trace without re-deriving ids.
#
# Why this can be done in workflow code at all
# --------------------------------------------
# Emitting spans from a workflow would normally be a determinism hazard: workflow code
# re-executes on replay, so every span would be duplicated. Temporal's ``OpenTelemetryPlugin``
# + ``create_tracer_provider()`` solve this — the returned ``ReplaySafeTracerProvider`` wraps
# every span so ``end()`` is suppressed during replay, and derives span/trace ids from
# ``workflow.new_random()`` so they are stable across replays. The plugin ALSO propagates the
# active span through Temporal headers into activities and child workflows, which is what makes
# a model span opened inside an activity (a different process) nest under the turn span opened
# in the workflow, with no context plumbing of our own.
#
# Nothing here is required. If ``opentelemetry`` is not installed, or no tracer provider is
# configured, every helper returns a no-op span and the harness behaves exactly as before — so
# the core package keeps its "temporalio + pydantic only" dependency posture. Install the
# ``evals`` extra and call
# ``temporal_agent_harness.evals.tracing_plugin()`` to turn it on.
#
# KNOWN LIMITATION: ``ReplaySafeSpan.end()`` is suppressed during replay, so a span opened
# before a worker crash is never exported — its ``end()`` lands on the replay. A turn whose
# worker restarts mid-flight therefore loses its trace. This is bounded in practice (turn spans
# are short) EXCEPT for turns parked for a long time on a human approval. The AgentEvent stream
# remains the durable record of truth in that case; only the trace is lost.

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

from temporalio import activity, workflow

try:  # pragma: no cover - exercised by the tracing-absent test
    from opentelemetry import trace as _otel
    from opentelemetry.trace import SpanKind, StatusCode
    from temporalio.contrib.opentelemetry._tracer_provider import (
        ReplaySafeTracerProvider,
    )

    _OTEL_IMPORTED = True
except ImportError:  # pragma: no cover - depends on install extras
    _otel = None  # type: ignore[assignment]
    SpanKind = None  # type: ignore[assignment]
    StatusCode = None  # type: ignore[assignment]
    ReplaySafeTracerProvider = ()  # type: ignore[assignment,misc]
    _OTEL_IMPORTED = False


_TRACER_NAME: Final = "temporal_agent_harness"

# Identifiers for the AI SDK behind a model call. Constants rather than bare strings because
# the same name has to match on both sides of the double-count guard below — the integration
# that emits the span, and the setup code that says "an SDK-native instrumentor is also tracing
# this SDK's calls". A typo would silently double the reported cost.
SDK_OPENAI_AGENTS: Final = "openai_agents"
SDK_PYDANTIC_AI: Final = "pydantic_ai"
SDK_GOOGLE_GENAI: Final = "google_genai"

# SDKs whose own instrumentation is installed in THIS worker process (see
# ``mark_sdk_instrumented``). Process-global, like every other piece of OTel instrumentation
# state, and set once at worker startup next to the instrumentor it describes.
_EXTERNALLY_INSTRUMENTED: set[str] = set()


def mark_sdk_instrumented(sdk: str) -> None:
    """Declare that ``sdk``'s own OTel instrumentation is active in this process.

    The harness then stops claiming that SDK's token usage under the ``gen_ai.usage.*``
    semantic-convention keys, because the SDK's own generation span is already reporting the
    same tokens and a backend that sums over both would report double the real cost.

    Scoped per SDK rather than globally on purpose: one worker can host a Gemini agent and an
    OpenAI-Agents agent side by side, and instrumenting one must not silently stop the other's
    cost from being counted.
    """
    _EXTERNALLY_INSTRUMENTED.add(sdk)


def is_sdk_instrumented(sdk: str | None) -> bool:
    return sdk is not None and sdk in _EXTERNALLY_INSTRUMENTED


def clear_sdk_instrumentation() -> None:
    """Forget every :func:`mark_sdk_instrumented` call (tests)."""
    _EXTERNALLY_INSTRUMENTED.clear()

# Cap on any single string attribute. Traces are not a data warehouse: a runaway tool output
# or a long conversation can otherwise push a span past a collector's payload limit and get the
# WHOLE span dropped, which is a far worse failure than a truncated value. Truncated values are
# suffixed so a reader can tell.
MAX_ATTRIBUTE_CHARS: Final = 8_000
_TRUNCATION_SUFFIX: Final = "…[truncated]"


class GenAI:
    """Attribute names, kept in one place so the semantic-convention vocabulary is greppable.

    Two families are used deliberately:

    * ``gen_ai.*`` — OpenTelemetry's GenAI semantic conventions. Langfuse (and other OTLP
      backends) convert spans carrying these into *generations* with model, token and cost
      accounting, automatically. This is why the model span is worth naming carefully.
    * ``input.value`` / ``output.value`` — the OpenInference convention for an observation's
      input and output payloads, which Langfuse also understands. OTel's GenAI conventions have
      no stable general-purpose equivalent, so these carry turn and tool payloads.

    ``temporal.*`` are our own, and are the reason a Temporal-hosted agent's trace can show
    something nobody else's can: which attempt a model call ran on.
    """

    OPERATION_NAME: Final = "gen_ai.operation.name"
    REQUEST_MODEL: Final = "gen_ai.request.model"
    RESPONSE_MODEL: Final = "gen_ai.response.model"
    USAGE_INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"
    USAGE_TOTAL_TOKENS: Final = "gen_ai.usage.total_tokens"
    TOOL_NAME: Final = "gen_ai.tool.name"
    TOOL_CALL_ID: Final = "gen_ai.tool.call.id"

    INPUT_VALUE: Final = "input.value"
    OUTPUT_VALUE: Final = "output.value"

    # Caller-supplied labels are namespaced under this prefix. The harness does not validate
    # label keys, so writing them bare would let a caller silently overwrite a semantic
    # convention attribute (a label literally named "gen_ai.request.model" would corrupt the
    # span's model, and therefore its cost accounting).
    LABEL_PREFIX: Final = "tnh.label."

    AGENT_ID: Final = "tnh.agent_id"
    TURN_ID: Final = "tnh.turn_id"
    TURN_NUMBER: Final = "tnh.turn_number"
    SUBAGENT_ID: Final = "tnh.subagent_id"
    APPROVAL_GRANTED: Final = "tnh.approval.granted"
    APPROVAL_REASON: Final = "tnh.approval.reason"

    TEMPORAL_ATTEMPT: Final = "temporal.attempt"
    TEMPORAL_ACTIVITY_TYPE: Final = "temporal.activity_type"

    # Non-semconv mirrors of the usage numbers. Used INSTEAD of ``gen_ai.usage.*`` when an
    # SDK-native instrumentation is also recording this model call, so the backend does not
    # count the same tokens twice while the numbers stay visible on our span.
    UNBILLED_INPUT_TOKENS: Final = "tnh.usage.input_tokens"
    UNBILLED_OUTPUT_TOKENS: Final = "tnh.usage.output_tokens"
    UNBILLED_TOTAL_TOKENS: Final = "tnh.usage.total_tokens"
    # Set only when this span deliberately did NOT claim the tokens, naming who did.
    USAGE_BILLED_BY: Final = "tnh.usage.billed_by"

    #: Which AI SDK integration produced a model span (an ``SDK_*`` constant).
    SDK: Final = "tnh.sdk"


def _coerce(value: Any) -> str | bool | int | float:
    """Render ``value`` as something OTel accepts as an attribute.

    OTel attributes may only be scalars (or homogeneous sequences of them), so the dicts the
    harness deals in — a tool's input, a handler's reply model — are JSON-encoded. ``default=str``
    keeps a stray non-serializable value from raising: an attribute is never worth failing a turn
    over.
    """
    if isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, str):
        try:
            value = json.dumps(value, default=str, sort_keys=True)
        except (TypeError, ValueError):
            value = str(value)
    if len(value) > MAX_ATTRIBUTE_CHARS:
        return value[:MAX_ATTRIBUTE_CHARS] + _TRUNCATION_SUFFIX
    return value


def tracing_available() -> bool:
    """Whether span creation from the CURRENT context will actually record anything.

    False when ``opentelemetry`` is not installed, and — importantly — false inside a workflow
    whose global tracer provider is not a ``ReplaySafeTracerProvider``. Creating real spans from
    workflow code under an ordinary provider would emit a duplicate set on every replay, so the
    harness degrades to no-op spans rather than corrupting a trace. Outside a workflow (i.e. in
    an activity, where there is no replay) any configured provider is fine.
    """
    if not _OTEL_IMPORTED:
        return False
    if not workflow.in_workflow():
        return True
    return isinstance(_otel.get_tracer_provider(), ReplaySafeTracerProvider)


class AgentSpan:
    """Handle to one harness span, or to nothing at all.

    Callers never touch OpenTelemetry types directly: when tracing is off the helpers below hand
    back an instance wrapping ``None`` and every method is a no-op, so instrumentation call sites
    need no ``if tracing_enabled`` guards.

    :attr:`trace_id` / :attr:`span_id` are lowercase hex (32 / 16 chars) or ``""`` when there is
    no span. The runner stamps them onto ``TurnStarted`` — that is the join between this trace
    and the durable AgentEvent stream.
    """

    __slots__ = ("_span", "_usage_billable")

    def __init__(self, span: Any | None = None, *, usage_billable: bool = True) -> None:
        self._span = span
        # Whether THIS span should report usage under the semantic-convention keys a backend
        # sums cost over. Decided once, where the span is created (see :func:`model_span`), so
        # call sites just call ``set_usage`` and cannot get the double-count guard wrong.
        self._usage_billable = usage_billable

    @property
    def recording(self) -> bool:
        return self._context() is not None

    def _context(self) -> Any | None:
        """The span context, but only when it is a real one.

        With ``opentelemetry`` installed but no provider configured, ``get_tracer`` still hands
        back a tracer and ``start_as_current_span`` still yields a span — a *non-recording* one
        whose context is all zeroes. Treating that as a span would stamp
        ``otel_trace_id="000…0"`` onto every ``TurnStarted``, which is worse than an empty
        string: it looks like a real id and resolves to nothing.
        """
        if self._span is None:
            return None
        context = self._span.get_span_context()
        return context if context.is_valid else None

    @property
    def trace_id(self) -> str:
        context = self._context()
        return format(context.trace_id, "032x") if context else ""

    @property
    def span_id(self) -> str:
        context = self._context()
        return format(context.span_id, "016x") if context else ""

    def set(self, key: str, value: Any) -> None:
        """Set one attribute, coercing ``value`` and skipping ``None``."""
        if self._span is None or value is None:
            return
        self._span.set_attribute(key, _coerce(value))

    def set_many(self, attributes: Mapping[str, Any]) -> None:
        """Set several attributes; ``None`` values are skipped rather than recorded as null."""
        if self._span is None:
            return
        for key, value in attributes.items():
            self.set(key, value)

    def set_usage(
        self,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        billable: bool | None = None,
    ) -> None:
        """Record token usage on a model span.

        Unbilled usage is recorded under ``tnh.usage.*`` instead of ``gen_ai.usage.*``. That
        matters because backends sum cost over every span carrying the semantic-convention usage
        keys: when an SDK-native instrumentation is *also* tracing this call, both spans describe
        the same tokens and the trace would report double the real cost. The losing span keeps
        its numbers visible but out of the sum.

        ``billable`` defaults to the decision :func:`model_span` already made from the SDK's
        instrumentation state — pass it explicitly only to override that.
        """
        if self._usage_billable if billable is None else billable:
            keys = (
                GenAI.USAGE_INPUT_TOKENS,
                GenAI.USAGE_OUTPUT_TOKENS,
                GenAI.USAGE_TOTAL_TOKENS,
            )
        else:
            keys = (
                GenAI.UNBILLED_INPUT_TOKENS,
                GenAI.UNBILLED_OUTPUT_TOKENS,
                GenAI.UNBILLED_TOTAL_TOKENS,
            )
        for key, value in zip(keys, (input_tokens, output_tokens, total_tokens)):
            self.set(key, value)

    def set_input(self, value: Any) -> None:
        self.set(GenAI.INPUT_VALUE, value)

    def set_output(self, value: Any) -> None:
        self.set(GenAI.OUTPUT_VALUE, value)

    def record_error(self, message: str, *, exception: BaseException | None = None) -> None:
        """Mark the span failed.

        Used for outcomes the harness treats as *data* rather than as raised exceptions — a turn
        whose handler raised (the runner catches it and keeps the loop alive), a denied approval,
        a tool that returned an error. The span must still read as failed even though nothing
        propagates out of the ``with`` block.

        DO NOT "simplify" this by always calling ``record_exception``. Inside a workflow,
        Temporal's ``_ReplaySafeSpan.record_exception`` latches the exception and its ``end()``
        then DROPS the span entirely unless the exception is a Temporal failure exception — the
        assumption being that a recorded exception means the workflow task is about to fail and
        be retried, so emitting a span per attempt would duplicate telemetry. That assumption
        does not hold here: the runner catches handler failures on purpose to keep the session
        alive, the workflow task succeeds, and the span would silently never be exported. So in
        workflow code we set the status (which carries the message the backend displays) and
        record the exception type as an attribute instead of losing the span.
        """
        if self._span is None:
            return
        self._span.set_status(StatusCode.ERROR, message)
        if exception is None:
            return
        if workflow.in_workflow():
            self._span.set_attribute("exception.type", type(exception).__name__)
        else:
            self._span.record_exception(exception)


_NO_SPAN: Final = AgentSpan(None)


@contextmanager
def _span(
    name: str,
    *,
    kind: Any = None,
    attributes: Mapping[str, Any] | None = None,
    usage_billable: bool = True,
) -> Iterator[AgentSpan]:
    """Open one span as the current span, or yield a no-op handle when tracing is off.

    ``start_as_current_span`` (rather than ``start_span``) is load-bearing: making the span
    *current* is what lets Temporal's OTel plugin propagate it through activity and
    child-workflow headers, so spans created in another process nest underneath.
    """
    if not tracing_available():
        yield _NO_SPAN
        return
    tracer = _otel.get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span(
        name, kind=kind or SpanKind.INTERNAL, record_exception=False
    ) as raw:
        handle = AgentSpan(raw, usage_billable=usage_billable)
        if attributes:
            handle.set_many(attributes)
        try:
            yield handle
        except BaseException as exc:
            handle.record_error(str(exc) or type(exc).__name__, exception=exc)
            raise


@contextmanager
def turn_span(
    *,
    agent_id: str,
    turn_id: str,
    turn_number: int,
    user_message: str,
    labels: Mapping[str, str] | None = None,
) -> Iterator[AgentSpan]:
    """The root span for one agent turn — the unit an eval backend scores.

    A turn is the right trace root: it is bounded (unlike a session workflow, which parks
    indefinitely and may live for weeks), it has a clear input and a typed output, and it is the
    harness's own atomic unit. Everything the turn does — model calls, tool calls, approval
    waits, subagent turns, across however many processes — nests underneath.

    The caller is expected to read :attr:`AgentSpan.trace_id` and stamp it onto the turn's
    ``TurnStarted`` event, and to call :meth:`AgentSpan.set_output` with the handler's reply.

    ``labels`` are the caller's own metadata (see ``AgentConfig.labels``), written under the
    ``tnh.label.*`` prefix. Namespacing them keeps a caller-chosen key from ever colliding with
    a semantic-convention attribute — the harness does not validate label keys, so an
    unprefixed ``gen_ai.request.model`` label would otherwise silently corrupt the span.
    """
    with _span(
        "agent.turn",
        attributes={
            GenAI.OPERATION_NAME: "invoke_agent",
            GenAI.AGENT_ID: agent_id,
            GenAI.TURN_ID: turn_id,
            GenAI.TURN_NUMBER: turn_number,
            GenAI.INPUT_VALUE: user_message,
        },
    ) as span:
        for key, value in (labels or {}).items():
            span.set(f"{GenAI.LABEL_PREFIX}{key}", value)
        yield span


@contextmanager
def model_span(
    *,
    model: str | None,
    sdk: str | None = None,
    attempt: int | None = None,
    activity_type: str | None = None,
) -> Iterator[AgentSpan]:
    """One model interaction, named per GenAI semantic conventions (``chat {model}``).

    Opened inside the activity that makes the call, so it nests under the turn span in the
    workflow process automatically. ``attempt`` comes from ``activity.info().attempt``: a
    retried model call shows up as sibling spans with ``temporal.attempt`` 0, 1, 2 — visible
    flakiness that a non-durable agent framework cannot show you.

    ``sdk`` names the integration making the call (one of the ``SDK_*`` constants). It drives
    the double-count guard: if that SDK's own instrumentation is active in this process
    (:func:`mark_sdk_instrumented`), this span records usage outside the semantic-convention
    keys so the SDK's generation span is the only one a backend bills for.
    """
    billable = not is_sdk_instrumented(sdk)
    with _span(
        f"chat {model}" if model else "chat",
        kind=SpanKind.CLIENT if _OTEL_IMPORTED else None,
        attributes={
            GenAI.OPERATION_NAME: "chat",
            GenAI.REQUEST_MODEL: model,
            GenAI.SDK: sdk,
            GenAI.TEMPORAL_ATTEMPT: attempt,
            GenAI.TEMPORAL_ACTIVITY_TYPE: activity_type,
            # Says WHY the usage is not under gen_ai.usage.* — otherwise a reader looking at a
            # generation with no billable tokens has no way to tell this was deliberate.
            GenAI.USAGE_BILLED_BY: None if billable else f"{sdk}-instrumentation",
        },
        usage_billable=billable,
    ) as span:
        yield span


@contextmanager
def tool_span(*, tool_name: str, tool_id: str, tool_input: Any = None) -> Iterator[AgentSpan]:
    """One tool call, from dispatch through completion.

    Opened at the point the call is *requested* rather than when it starts executing, so any
    time spent waiting on a human approval falls INSIDE the span. That is deliberate: for an
    agent with a human in the loop, the gap between "the model asked" and "the tool ran" is
    usually the most interesting number in the trace.
    """
    with _span(
        f"execute_tool {tool_name}",
        attributes={
            GenAI.OPERATION_NAME: "execute_tool",
            GenAI.TOOL_NAME: tool_name,
            GenAI.TOOL_CALL_ID: tool_id,
            GenAI.INPUT_VALUE: tool_input,
        },
    ) as span:
        yield span


@contextmanager
def approval_span(*, tool_name: str, tool_id: str) -> Iterator[AgentSpan]:
    """The human-approval gate for one tool call, nested inside its tool span.

    The span's duration is how long the agent waited on a person — which is the
    human-in-the-loop latency metric, and is otherwise invisible.
    """
    with _span(
        "agent.tool_approval",
        attributes={GenAI.TOOL_NAME: tool_name, GenAI.TOOL_CALL_ID: tool_id},
    ) as span:
        yield span


@contextmanager
def callback_span(*, tool_name: str, tool_id: str) -> Iterator[AgentSpan]:
    """The wait for an external client to execute a callback tool, inside its tool span.

    Kept separate from :func:`approval_span` even though both are just wall-clock waits on
    something outside the worker: "waited for a human to say yes" and "waited for the user's
    laptop to run the tool" are different problems with different fixes, and a tool span that
    merged them would show a long duration with no way to tell which.
    """
    with _span(
        "agent.callback",
        attributes={GenAI.TOOL_NAME: tool_name, GenAI.TOOL_CALL_ID: tool_id},
    ) as span:
        yield span


@contextmanager
def subagent_span(*, subagent_id: str, function: str, payload: Any = None) -> Iterator[AgentSpan]:
    """One turn a parent agent drives on a subagent.

    The child runs as its own workflow, so its own ``agent.turn`` span nests under this one via
    child-workflow context propagation — the whole agent tree becomes one trace.
    """
    with _span(
        f"subagent {function}",
        attributes={
            GenAI.SUBAGENT_ID: subagent_id,
            GenAI.INPUT_VALUE: payload,
        },
    ) as span:
        yield span


def activity_context() -> tuple[int | None, str | None]:
    """``(attempt, activity_type)`` when running inside an activity, else ``(None, None)``.

    Shared by every SDK integration so all three derive the retry attribute identically —
    ``temporal.attempt > 0`` on a model span is the filter that finds every flaky provider call
    in production, and it is only meaningful if it means the same thing everywhere.
    """
    if not activity.in_activity():
        return None, None
    info = activity.info()
    return info.attempt, info.activity_type


def current_trace_ids() -> tuple[str, str] | None:
    """``(trace_id, span_id)`` of the active span as lowercase hex, or ``None`` if unrecorded.

    Lets code that is not itself opening a span — an activity publishing an event, a client
    correlating a reply — reference the enclosing trace.
    """
    if not _OTEL_IMPORTED:
        return None
    span = _otel.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x"), format(context.span_id, "016x")


__all__ = [
    "MAX_ATTRIBUTE_CHARS",
    "SDK_GOOGLE_GENAI",
    "SDK_OPENAI_AGENTS",
    "SDK_PYDANTIC_AI",
    "AgentSpan",
    "GenAI",
    "activity_context",
    "approval_span",
    "callback_span",
    "clear_sdk_instrumentation",
    "current_trace_ids",
    "is_sdk_instrumented",
    "mark_sdk_instrumented",
    "model_span",
    "subagent_span",
    "tool_span",
    "tracing_available",
    "turn_span",
]
