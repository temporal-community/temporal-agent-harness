# ABOUTME: Tests for the tier-2 (SDK-native) instrumentation seam and the double-count guard.
#
# The guard is the thing worth pinning: when an SDK's own OTel instrumentation is active, BOTH
# it and the harness describe the same model call, and a backend that sums cost over every span
# carrying gen_ai.usage.* would report double the real spend. The harness's span has to yield
# the semantic-convention keys — per SDK, since one worker can host several.
#
# Also verifies the premise tier 2 rests on: a span created by an unrelated instrumentation
# inside an activity nests under the harness's model span, purely through OTel's ambient
# context, with no correlation scheme of ours.
#
# Run with: uv run pytest tests/harness/test_tier2_instrumentation.py -v

from __future__ import annotations

import opentelemetry.trace
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio.contrib.opentelemetry import create_tracer_provider

from temporal_agent_harness.harness import tracing


@pytest.fixture
def span_exporter():
    previous = opentelemetry.trace._TRACER_PROVIDER
    exporter = InMemorySpanExporter()
    provider = create_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    opentelemetry.trace._TRACER_PROVIDER = provider
    try:
        yield exporter
    finally:
        opentelemetry.trace._TRACER_PROVIDER = previous


@pytest.fixture(autouse=True)
def clean_instrumentation_registry():
    # Process-global, like all OTel instrumentation state — so it must be reset between tests
    # or one test's marking silently changes another's billing.
    tracing.clear_sdk_instrumentation()
    yield
    tracing.clear_sdk_instrumentation()


def _named(exporter: InMemorySpanExporter, name: str):
    return [s for s in exporter.get_finished_spans() if s.name == name]


# ---------------------------------------------------------------------------
# The double-count guard
# ---------------------------------------------------------------------------


def test_usage_is_billable_by_default(span_exporter):
    with tracing.model_span(model="m", sdk=tracing.SDK_PYDANTIC_AI) as span:
        span.set_usage(input_tokens=10, output_tokens=5, total_tokens=15)

    attrs = _named(span_exporter, "chat m")[0].attributes
    # Nothing else is tracing this call, so the harness is the only reporter and must bill.
    assert attrs[tracing.GenAI.USAGE_INPUT_TOKENS] == 10
    assert tracing.GenAI.UNBILLED_INPUT_TOKENS not in attrs
    assert tracing.GenAI.USAGE_BILLED_BY not in attrs


def test_marking_an_sdk_moves_its_usage_out_of_the_billed_keys(span_exporter):
    tracing.mark_sdk_instrumented(tracing.SDK_PYDANTIC_AI)

    with tracing.model_span(model="m", sdk=tracing.SDK_PYDANTIC_AI) as span:
        span.set_usage(input_tokens=10, output_tokens=5, total_tokens=15)

    attrs = _named(span_exporter, "chat m")[0].attributes
    # The SDK's own generation span reports these tokens; ours must not, or the trace's cost
    # doubles. The numbers stay visible, just not under the keys a backend sums.
    assert tracing.GenAI.USAGE_INPUT_TOKENS not in attrs
    assert tracing.GenAI.USAGE_TOTAL_TOKENS not in attrs
    assert attrs[tracing.GenAI.UNBILLED_INPUT_TOKENS] == 10
    assert attrs[tracing.GenAI.UNBILLED_TOTAL_TOKENS] == 15
    # ...and the span says why, so a reader isn't left wondering where the tokens went.
    assert attrs[tracing.GenAI.USAGE_BILLED_BY] == "pydantic_ai-instrumentation"


def test_the_guard_is_scoped_per_sdk(span_exporter):
    # One worker can host a Gemini agent and a Pydantic AI agent. Instrumenting one must not
    # silently stop the other's cost from being counted.
    tracing.mark_sdk_instrumented(tracing.SDK_PYDANTIC_AI)

    with tracing.model_span(model="pyd", sdk=tracing.SDK_PYDANTIC_AI) as span:
        span.set_usage(input_tokens=1, output_tokens=1, total_tokens=2)
    with tracing.model_span(model="gem", sdk=tracing.SDK_GOOGLE_GENAI) as span:
        span.set_usage(input_tokens=3, output_tokens=4, total_tokens=7)

    pyd = _named(span_exporter, "chat pyd")[0].attributes
    gem = _named(span_exporter, "chat gem")[0].attributes
    assert tracing.GenAI.USAGE_TOTAL_TOKENS not in pyd
    assert gem[tracing.GenAI.USAGE_TOTAL_TOKENS] == 7


def test_an_unknown_sdk_is_always_billable(span_exporter):
    tracing.mark_sdk_instrumented(tracing.SDK_PYDANTIC_AI)

    with tracing.model_span(model="m", sdk=None) as span:
        span.set_usage(input_tokens=2, output_tokens=2, total_tokens=4)

    # Failing OPEN is the right default: under-reporting cost is a silent, expensive error,
    # while over-reporting is visible and gets fixed.
    assert _named(span_exporter, "chat m")[0].attributes[
        tracing.GenAI.USAGE_TOTAL_TOKENS
    ] == 4


def test_an_explicit_billable_argument_still_wins(span_exporter):
    with tracing.model_span(model="m", sdk=tracing.SDK_PYDANTIC_AI) as span:
        span.set_usage(
            input_tokens=1, output_tokens=1, total_tokens=2, billable=False
        )

    attrs = _named(span_exporter, "chat m")[0].attributes
    assert tracing.GenAI.USAGE_TOTAL_TOKENS not in attrs
    assert attrs[tracing.GenAI.UNBILLED_TOTAL_TOKENS] == 2


def test_the_sdk_is_recorded_on_the_span(span_exporter):
    with tracing.model_span(model="m", sdk=tracing.SDK_OPENAI_AGENTS):
        pass
    assert (
        _named(span_exporter, "chat m")[0].attributes[tracing.GenAI.SDK]
        == "openai_agents"
    )


# ---------------------------------------------------------------------------
# The premise: a foreign instrumentation's spans nest under ours
# ---------------------------------------------------------------------------


def test_a_foreign_span_nests_under_the_model_span(span_exporter):
    """Tier 2 needs no correlation scheme — it just has to be inside the right span.

    Stands in for what an SDK's instrumentor does: create an ordinary OTel span while the
    harness's model span is current. Nothing links them explicitly; OTel's ambient context does
    it, which is the same mechanism that carries the turn span into the activity in the first
    place.
    """
    tracer = opentelemetry.trace.get_tracer("pretend-sdk-instrumentor")
    with tracing.model_span(model="m", sdk=tracing.SDK_PYDANTIC_AI) as harness_span:
        with tracer.start_as_current_span("chat gpt-x") as sdk_span:
            sdk_span.set_attribute("gen_ai.prompt", "you are a helpful assistant")

        harness_trace_id = harness_span.trace_id

    foreign = _named(span_exporter, "chat gpt-x")[0]
    ours = _named(span_exporter, "chat m")[0]
    assert foreign.parent.span_id == ours.context.span_id
    assert format(foreign.context.trace_id, "032x") == harness_trace_id
    # The content the harness deliberately does NOT carry rides on the SDK's span instead.
    assert foreign.attributes["gen_ai.prompt"] == "you are a helpful assistant"


# ---------------------------------------------------------------------------
# The setup helpers
# ---------------------------------------------------------------------------


def test_instrument_pydantic_ai_enables_content_and_marks_the_sdk():
    from pydantic_ai import Agent

    from temporal_agent_harness.evals.tier2 import instrument_pydantic_ai

    try:
        settings = instrument_pydantic_ai(include_content=True)
        # Both halves must happen together: instrumenting without marking is exactly the
        # double-count bug this module exists to prevent.
        assert settings.include_content is True
        assert tracing.is_sdk_instrumented(tracing.SDK_PYDANTIC_AI)
    finally:
        Agent.instrument_all(False)


def test_marking_openai_agents_without_installing_an_instrumentor():
    from temporal_agent_harness.evals.tier2 import mark_openai_agents_instrumented

    # The escape hatch for when OpenAIAgentsPlugin(use_otel_instrumentation=True) installed the
    # instrumentor instead of us.
    mark_openai_agents_instrumented()
    assert tracing.is_sdk_instrumented(tracing.SDK_OPENAI_AGENTS)
    assert not tracing.is_sdk_instrumented(tracing.SDK_GOOGLE_GENAI)
