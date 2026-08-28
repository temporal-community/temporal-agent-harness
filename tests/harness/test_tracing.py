# ABOUTME: Tests for the harness's OpenTelemetry seam — that a turn produces one replay-safe
# root span carrying the turn's input/output, that a failed turn marks the span errored, and
# that the trace id stamped onto the TurnStarted event is the id of that very span (the join
# between the durable AgentEvent stream and the trace an eval backend sees).
#
# Also pins the no-op contract: with no tracer provider configured the harness must behave
# exactly as before and must NOT stamp a bogus all-zero trace id.
#
# Spans are asserted against an InMemorySpanExporter rather than a live backend, so these run
# offline and deterministically.
#
# Run with: uv run pytest tests/harness/test_tracing.py -v

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import opentelemetry.trace
import pytest
import pytest_asyncio
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from temporalio import workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.opentelemetry import OpenTelemetryPlugin, create_tracer_provider
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import AgentWorkflowRunner, agent, tracing
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    AGENT_STATUS_QUERY,
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentStatus,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)


@agent.tool_defn()
async def echo_tool(text: str) -> str:
    """An inline tool, so the probe needs no activity registration."""
    return f"tool:{text}"


@agent.activity_tool_defn(
    activity_config=ActivityConfig(start_to_close_timeout=timedelta(seconds=30))
)
async def traced_activity_tool(text: str) -> str:
    """An activity tool whose body opens a model span, exactly as the SDK observers do.

    This stands in for a real model call: what matters is that the span is created in the
    ACTIVITY, on the far side of a Temporal boundary from the workflow that opened the turn
    and tool spans.
    """
    attempt, activity_type = tracing.activity_context()
    with tracing.model_span(
        model="fake-model", attempt=attempt, activity_type=activity_type
    ) as span:
        span.set_usage(input_tokens=1, output_tokens=2, total_tokens=3)
    return f"act:{text}"


@workflow.defn
@agent.defn
class TracingProbeAgent:
    """Minimal model-free agent: one handler that echoes, one that raises."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Echo the message back."""
        return TextReply(text=f"echo:{message.text}")

    @agent.accepts
    async def boom(self, message: TextMessage) -> TextReply:
        """Always raises, to prove an errored turn still closes its span as failed."""
        raise RuntimeError(f"boom: {message.text}")

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Run the tool scenario named by the message text."""
        if message.text == "concurrent":
            results = await asyncio.gather(
                self._runner.run_tool("t-A", echo_tool, "A"),
                self._runner.run_tool("t-B", echo_tool, "B"),
            )
            return TextReply(text="|".join(results))
        if message.text == "activity":
            return TextReply(
                text=await self._runner.run_tool("t-act", traced_activity_tool, "X")
            )
        try:
            out = await self._runner.run_tool("t-1", echo_tool, message.text)
        except agent.ToolApprovalDenied as e:
            # Mirrors a real agent loop: a denial is a tool result, not a turn failure.
            out = f"denied:{e.reason}"
        return TextReply(text=out)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def span_exporter():
    """Install a replay-safe tracer provider exporting into memory, then restore.

    The global tracer provider is write-once in OpenTelemetry — ``set_tracer_provider`` warns
    and keeps the first one — so a fixture that merely called it would silently leak this
    provider into every later test in the session. Saving and restoring the module global is
    the standard way around that; it is private API, but the alternative is cross-test
    pollution that only shows up as confusing span counts much later.
    """
    previous = opentelemetry.trace._TRACER_PROVIDER
    exporter = InMemorySpanExporter()
    provider = create_tracer_provider()
    # Simple (not batched) so a span is exported the instant it ends and the test can assert
    # without flushing or sleeping.
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    opentelemetry.trace._TRACER_PROVIDER = provider
    try:
        yield exporter
    finally:
        opentelemetry.trace._TRACER_PROVIDER = previous


@pytest_asyncio.fixture
async def traced_client_and_queue(span_exporter):
    """Time-skipping env whose CLIENT carries the OTel plugin (workers inherit it)."""
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
        plugins=[OpenTelemetryPlugin()],
    )
    task_queue = f"tracing-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[TracingProbeAgent],
        activities=[agent.tool_activity(traced_activity_tool)],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue, span_exporter
        finally:
            await env.shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _start(
    client: Client, task_queue: str, config: AgentConfig | None = None
) -> WorkflowHandle:
    return await client.start_workflow(
        TracingProbeAgent.run,
        config or AgentConfig(),
        id=f"TracingProbeAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def _send(handle: WorkflowHandle, msg_type: str, payload: dict[str, Any]) -> None:
    status = await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(
            type=msg_type,
            payload=payload,
            expected_turn=status.current_turn + len(status.pending_turns) + 1,
        ),
    )


async def _collect_until_turn_end(client: Client, workflow_id: str) -> list[AgentEvent]:
    stream = WorkflowStreamClient.create(client, workflow_id)
    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in stream.subscribe(
            topics=["turn_events"],
            from_offset=0,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            events.append(item.data)
            if item.data.event.type == AgentEventType.TURN_END:
                break
    return events


def _turn_spans(exporter: InMemorySpanExporter):
    return [s for s in exporter.get_finished_spans() if s.name == "agent.turn"]


async def _await_turn_spans(exporter: InMemorySpanExporter, count: int, timeout: float = 10.0):
    """Wait for ``count`` turn spans to be exported.

    The turn span closes fractionally AFTER ``turn_end`` reaches the stream — ``turn_end`` is
    published from the runner's ``finally``, which is still inside the span's ``with`` block —
    so a test that read the stream to ``turn_end`` cannot assert on spans synchronously. That
    ordering is correct (the span should cover the whole turn, publish included); it just means
    "turn ended" and "trace exported" are two different moments, as they always are in
    production where export is batched.
    """
    async with asyncio.timeout(timeout):
        while len(_turn_spans(exporter)) < count:
            await asyncio.sleep(0.01)
    return _turn_spans(exporter)


# ---------------------------------------------------------------------------
# End-to-end span tests
# ---------------------------------------------------------------------------


async def test_turn_emits_one_root_span_with_input_and_output(traced_client_and_queue):
    client, task_queue, exporter = traced_client_and_queue
    handle = await _start(client, task_queue)
    await _send(handle, "ask", {"text": "hello"})
    await _collect_until_turn_end(client, handle.id)

    spans = await _await_turn_spans(exporter, 1)
    assert len(spans) == 1, "a turn must produce exactly one root span, even across replays"
    span = spans[0]
    attrs = span.attributes
    assert attrs[tracing.GenAI.OPERATION_NAME] == "invoke_agent"
    assert attrs[tracing.GenAI.TURN_NUMBER] == 1
    assert attrs[tracing.GenAI.TURN_ID]
    assert attrs[tracing.GenAI.AGENT_ID]
    assert "hello" in attrs[tracing.GenAI.INPUT_VALUE]
    # The output is the handler's typed reply model, serialized.
    assert "echo:hello" in attrs[tracing.GenAI.OUTPUT_VALUE]
    assert span.status.status_code is not StatusCode.ERROR


async def test_turn_started_event_carries_the_span_trace_id(traced_client_and_queue):
    client, task_queue, exporter = traced_client_and_queue
    handle = await _start(client, task_queue)
    await _send(handle, "ask", {"text": "hello"})
    events = await _collect_until_turn_end(client, handle.id)

    started = next(
        e.event for e in events if e.event.type == AgentEventType.TURN_STARTED
    )
    span = (await _await_turn_spans(exporter, 1))[0]

    # This equality IS the feature: it is what lets an out-of-band consumer take a turn off
    # the durable event stream and attach a score to the right trace.
    assert started.otel_trace_id == format(span.context.trace_id, "032x")
    assert started.otel_span_id == format(span.context.span_id, "016x")
    assert len(started.otel_trace_id) == 32
    assert int(started.otel_trace_id, 16) != 0


async def test_failed_turn_marks_the_span_errored(traced_client_and_queue):
    client, task_queue, exporter = traced_client_and_queue
    handle = await _start(client, task_queue)
    await _send(handle, "boom", {"text": "kaboom"})
    events = await _collect_until_turn_end(client, handle.id)

    # The runner swallows handler failures to keep the session alive...
    assert any(e.event.type == AgentEventType.ERROR for e in events)
    assert any(e.event.type == AgentEventType.TURN_END for e in events)

    # ...so the span has to be marked failed explicitly; nothing propagates out of the block.
    span = (await _await_turn_spans(exporter, 1))[0]
    assert span.status.status_code is StatusCode.ERROR
    assert "kaboom" in span.status.description
    # Recorded as an ATTRIBUTE, not via span.record_exception(): inside a workflow, Temporal's
    # replay-safe span drops any span that recorded a non-failure exception, on the assumption
    # the workflow task is about to be retried. Here the task succeeds (the runner swallows
    # handler errors by design), so calling record_exception would lose the span entirely.
    assert span.attributes["exception.type"] == "RuntimeError"
    assert span.events == ()


async def test_each_turn_gets_its_own_trace(traced_client_and_queue):
    client, task_queue, exporter = traced_client_and_queue
    handle = await _start(client, task_queue)
    await _send(handle, "ask", {"text": "one"})
    await _collect_until_turn_end(client, handle.id)
    await _send(handle, "ask", {"text": "two"})
    await _collect_until_turn_end(client, handle.id)

    spans = await _await_turn_spans(exporter, 2)
    assert len(spans) == 2
    # A turn is the trace root, not the session: a session workflow parks indefinitely and a
    # span covering it would never close.
    assert spans[0].context.trace_id != spans[1].context.trace_id
    assert {s.attributes[tracing.GenAI.TURN_NUMBER] for s in spans} == {1, 2}


async def test_labels_land_on_the_turn_span_namespaced(traced_client_and_queue):
    client, task_queue, exporter = traced_client_and_queue
    handle = await _start(
        client, task_queue, AgentConfig(labels={"experiment": "exp-1"})
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(
            type="ask",
            payload={"text": "hello"},
            expected_turn=1,
            labels={"dataset_item_id": "item-7"},
        ),
    )
    await _collect_until_turn_end(client, handle.id)

    attrs = (await _await_turn_spans(exporter, 1))[0].attributes
    # Namespaced: the harness never validates label keys, so writing them bare would let a
    # caller-chosen key collide with (and silently corrupt) a semantic-convention attribute.
    assert attrs["tnh.label.experiment"] == "exp-1"
    assert attrs["tnh.label.dataset_item_id"] == "item-7"


async def test_a_label_cannot_overwrite_a_semantic_attribute(traced_client_and_queue):
    client, task_queue, exporter = traced_client_and_queue
    handle = await _start(
        client,
        task_queue,
        # A caller doing this is almost certainly confused, but it must not be able to
        # rewrite the span's model — that would corrupt the backend's cost accounting.
        AgentConfig(labels={tracing.GenAI.REQUEST_MODEL: "not-a-real-model"}),
    )
    await _send(handle, "ask", {"text": "hello"})
    await _collect_until_turn_end(client, handle.id)

    attrs = (await _await_turn_spans(exporter, 1))[0].attributes
    assert attrs[f"tnh.label.{tracing.GenAI.REQUEST_MODEL}"] == "not-a-real-model"
    assert tracing.GenAI.REQUEST_MODEL not in attrs


# ---------------------------------------------------------------------------
# Tool / approval spans
# ---------------------------------------------------------------------------


def _named(exporter: InMemorySpanExporter, name: str):
    return [s for s in exporter.get_finished_spans() if s.name == name]


async def test_tool_span_nests_under_the_turn(traced_client_and_queue):
    client, task_queue, exporter = traced_client_and_queue
    handle = await _start(client, task_queue)
    await _send(handle, "act", {"text": "hello"})
    await _collect_until_turn_end(client, handle.id)
    await _await_turn_spans(exporter, 1)

    turn = _named(exporter, "agent.turn")[0]
    tool = _named(exporter, "execute_tool echo_tool")[0]
    assert tool.parent.span_id == turn.context.span_id
    assert tool.context.trace_id == turn.context.trace_id
    assert tool.attributes[tracing.GenAI.TOOL_NAME] == "echo_tool"
    assert tool.attributes[tracing.GenAI.TOOL_CALL_ID] == "t-1"
    assert "hello" in tool.attributes[tracing.GenAI.INPUT_VALUE]
    assert tool.attributes[tracing.GenAI.OUTPUT_VALUE] == "tool:hello"


async def test_auto_approved_call_has_no_approval_span(traced_client_and_queue):
    client, task_queue, exporter = traced_client_and_queue
    # dangerously_skip_all is the probe's default, so nothing gates.
    handle = await _start(client, task_queue)
    await _send(handle, "act", {"text": "hello"})
    await _collect_until_turn_end(client, handle.id)
    await _await_turn_spans(exporter, 1)

    # Only GATED calls get an approval span — otherwise every trace fills with
    # zero-duration approvals and the real human waits get lost among them.
    assert _named(exporter, "agent.tool_approval") == []


async def test_gated_call_records_the_human_wait(traced_client_and_queue):
    client, task_queue, exporter = traced_client_and_queue
    handle = await _start(
        client,
        task_queue,
        AgentConfig(approval_policy=ToolApprovalPolicy.always_require_approvals()),
    )
    agent_client = AgentClient(client, handle.id)
    await _send(handle, "act", {"text": "hello"})

    stream = WorkflowStreamClient.create(client, handle.id)
    async with asyncio.timeout(30):
        async for item in stream.subscribe(
            topics=["turn_events"],
            from_offset=0,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            ev = item.data
            if (
                ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == "t-1"
            ):
                await agent_client.approve_tool("t-1", approved=True)
            if ev.event.type == AgentEventType.TURN_END:
                break
    await _await_turn_spans(exporter, 1)

    approval = _named(exporter, "agent.tool_approval")[0]
    tool = _named(exporter, "execute_tool echo_tool")[0]
    # Nested inside the tool span, which is why the tool span opens before the gate: the
    # wait is charged to the tool call rather than falling into a gap between spans.
    assert approval.parent.span_id == tool.context.span_id
    assert approval.attributes[tracing.GenAI.APPROVAL_GRANTED] is True
    assert approval.end_time <= tool.end_time
    assert tool.start_time <= approval.start_time


async def test_denied_call_marks_approval_and_tool_spans(traced_client_and_queue):
    client, task_queue, exporter = traced_client_and_queue
    handle = await _start(
        client,
        task_queue,
        AgentConfig(approval_policy=ToolApprovalPolicy.always_require_approvals()),
    )
    agent_client = AgentClient(client, handle.id)
    await _send(handle, "act", {"text": "hello"})

    stream = WorkflowStreamClient.create(client, handle.id)
    async with asyncio.timeout(30):
        async for item in stream.subscribe(
            topics=["turn_events"],
            from_offset=0,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            ev = item.data
            if (
                ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == "t-1"
            ):
                await agent_client.approve_tool(
                    "t-1", approved=False, reason="not allowed"
                )
            if ev.event.type == AgentEventType.TURN_END:
                break
    await _await_turn_spans(exporter, 1)

    approval = _named(exporter, "agent.tool_approval")[0]
    assert approval.attributes[tracing.GenAI.APPROVAL_GRANTED] is False
    assert approval.attributes[tracing.GenAI.APPROVAL_REASON] == "not allowed"
    # The denial is the approval's *outcome*, not a failure of the gate itself...
    assert approval.status.status_code is not StatusCode.ERROR
    # ...but the tool call did fail, and the turn survived it.
    tool = _named(exporter, "execute_tool echo_tool")[0]
    assert tool.status.status_code is StatusCode.ERROR
    turn = _named(exporter, "agent.turn")[0]
    assert turn.status.status_code is not StatusCode.ERROR


async def test_activity_side_span_nests_under_the_workflow_side_turn(
    traced_client_and_queue,
):
    """The architectural premise: a span created in an ACTIVITY joins the turn's trace.

    Nothing in the harness propagates trace context — Temporal's OTel plugin carries the
    active span across the boundary in the request headers. This is why the SDK integrations
    need no ID scheme and no cross-turn plumbing: the model call simply runs inside the turn
    span, in whichever process it happens to land.
    """
    client, task_queue, exporter = traced_client_and_queue
    handle = await _start(client, task_queue)
    await _send(handle, "act", {"text": "activity"})
    await _collect_until_turn_end(client, handle.id)
    await _await_turn_spans(exporter, 1)

    turn = _named(exporter, "agent.turn")[0]
    tool = _named(exporter, "execute_tool traced_activity_tool")[0]
    model = _named(exporter, "chat fake-model")[0]

    assert model.context.trace_id == turn.context.trace_id
    assert model.parent.span_id == tool.context.span_id
    assert tool.parent.span_id == turn.context.span_id
    # The activity really did run as an activity, and said so.
    assert model.attributes[tracing.GenAI.TEMPORAL_ATTEMPT] == 1
    assert model.attributes[tracing.GenAI.TEMPORAL_ACTIVITY_TYPE] == "traced_activity_tool"
    assert model.attributes[tracing.GenAI.USAGE_TOTAL_TOKENS] == 3


async def test_concurrent_tool_calls_get_sibling_spans(traced_client_and_queue):
    client, task_queue, exporter = traced_client_and_queue
    handle = await _start(client, task_queue)
    await _send(handle, "act", {"text": "concurrent"})
    await _collect_until_turn_end(client, handle.id)
    await _await_turn_spans(exporter, 1)

    turn = _named(exporter, "agent.turn")[0]
    tools = _named(exporter, "execute_tool echo_tool")
    assert len(tools) == 2
    # Gathered calls run as separate asyncio tasks, so each gets its own contextvar copy —
    # the spans must be siblings under the turn, not accidentally nested in each other.
    assert {t.parent.span_id for t in tools} == {turn.context.span_id}
    assert {t.attributes[tracing.GenAI.TOOL_CALL_ID] for t in tools} == {"t-A", "t-B"}


# ---------------------------------------------------------------------------
# The no-op contract
# ---------------------------------------------------------------------------


async def test_untraced_agent_stamps_empty_ids_and_still_works():
    """With no tracer provider, the harness behaves exactly as before."""
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"untraced-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[TracingProbeAgent],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            handle = await _start(env.client, task_queue)
            await _send(handle, "ask", {"text": "hello"})
            events = await _collect_until_turn_end(env.client, handle.id)
        finally:
            await env.shutdown()

    started = next(
        e.event for e in events if e.event.type == AgentEventType.TURN_STARTED
    )
    reply = next(e.event for e in events if e.event.type == AgentEventType.REPLY)
    assert reply.output == {"text": "echo:hello"}
    # Empty, NOT an all-zero id: a zero id looks real and resolves to nothing.
    assert started.otel_trace_id == ""
    assert started.otel_span_id == ""


# ---------------------------------------------------------------------------
# Model spans (the shape the three SDK observers produce)
# ---------------------------------------------------------------------------


def test_model_span_carries_semconv_model_usage_and_attempt(span_exporter):
    with tracing.model_span(
        model="gemini-3-flash", attempt=2, activity_type="invoke_model"
    ) as span:
        span.set_usage(input_tokens=10, output_tokens=5, total_tokens=15)

    span_data = _named(span_exporter, "chat gemini-3-flash")[0]
    attrs = span_data.attributes
    assert attrs[tracing.GenAI.OPERATION_NAME] == "chat"
    assert attrs[tracing.GenAI.REQUEST_MODEL] == "gemini-3-flash"
    assert attrs[tracing.GenAI.USAGE_INPUT_TOKENS] == 10
    assert attrs[tracing.GenAI.USAGE_OUTPUT_TOKENS] == 5
    assert attrs[tracing.GenAI.USAGE_TOTAL_TOKENS] == 15
    # attempt > 0 means this model call was RETRIED — visible flakiness that only a durable
    # execution engine can surface.
    assert attrs[tracing.GenAI.TEMPORAL_ATTEMPT] == 2
    assert attrs[tracing.GenAI.TEMPORAL_ACTIVITY_TYPE] == "invoke_model"


def test_unbilled_usage_stays_out_of_the_cost_sum(span_exporter):
    with tracing.model_span(model="m") as span:
        span.set_usage(
            input_tokens=10, output_tokens=5, total_tokens=15, billable=False
        )

    attrs = _named(span_exporter, "chat m")[0].attributes
    # Backends sum cost over spans carrying the semconv usage keys. When an SDK-native
    # instrumentation also traces this call, ours must keep the numbers visible but unsummed,
    # or the trace reports double the real cost.
    assert tracing.GenAI.USAGE_INPUT_TOKENS not in attrs
    assert attrs[tracing.GenAI.UNBILLED_INPUT_TOKENS] == 10
    assert attrs[tracing.GenAI.UNBILLED_TOTAL_TOKENS] == 15


def test_missing_usage_fields_are_skipped_not_nulled(span_exporter):
    with tracing.model_span(model="m") as span:
        span.set_usage(input_tokens=None, output_tokens=3, total_tokens=None)

    attrs = _named(span_exporter, "chat m")[0].attributes
    assert tracing.GenAI.USAGE_INPUT_TOKENS not in attrs
    assert attrs[tracing.GenAI.USAGE_OUTPUT_TOKENS] == 3


def test_activity_context_is_empty_outside_an_activity():
    assert tracing.activity_context() == (None, None)


def test_no_op_span_handle_is_inert():
    span = tracing.AgentSpan(None)
    assert not span.recording
    assert span.trace_id == ""
    assert span.span_id == ""
    # Every mutator must tolerate being called on a no-op handle — that is what lets the
    # instrumentation call sites stay free of `if tracing_enabled` guards.
    span.set("k", "v")
    span.set_many({"a": 1})
    span.set_input({"x": 1})
    span.set_output("out")
    span.record_error("nope", exception=RuntimeError("nope"))


def test_span_helpers_are_inert_without_a_provider():
    """Outside a workflow with no provider configured, spans are non-recording."""
    with tracing.turn_span(
        agent_id="a1b2c3", turn_id="t", turn_number=1, user_message="hi"
    ) as span:
        assert span.trace_id == ""
        assert span.span_id == ""


def test_attribute_values_are_coerced_and_truncated():
    # Dicts are JSON-encoded, since OTel attributes may only be scalars.
    assert tracing._coerce({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
    assert tracing._coerce(7) == 7
    assert tracing._coerce(True) is True

    long = "x" * (tracing.MAX_ATTRIBUTE_CHARS + 500)
    coerced = tracing._coerce(long)
    # Truncated rather than dropped: an oversized attribute can cost you the whole span.
    assert len(coerced) == tracing.MAX_ATTRIBUTE_CHARS + len("…[truncated]")
    assert coerced.endswith("…[truncated]")


def test_unserializable_attribute_does_not_raise():
    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    # An attribute is never worth failing a turn over.
    assert "opaque" in tracing._coerce({"k": Opaque()})
