# ABOUTME: Opt-in SDK-native instrumentation — the detail tier that captures the actual prompts
# and completions the harness's own spans deliberately do not carry.
#
# Why a second tier exists at all
# -------------------------------
# The harness's spans (``harness/tracing.py``) describe the agent's SHAPE: turns, tool calls,
# approval waits, subagent hand-offs, token counts. What they cannot describe is the content of
# a model call, because the harness never sees it — the prompt is assembled inside the SDK, in
# the activity, and putting it on the event stream would republish the whole conversation on
# every model call into durable workflow history (see the "prompts are deliberately not on the
# stream" note in the design doc).
#
# An SDK's own instrumentation already captures exactly that. And because Temporal's OTel plugin
# has made the harness's model span *current* inside the activity, an SDK span created there
# nests underneath it automatically — no ID scheme, no context propagation, no cross-turn
# problem. That is the entire integration: turn the instrumentor on, and tell the harness you
# did so it stops claiming the same tokens.
#
# Never call an instrumentor directly and skip the ``mark_sdk_instrumented`` half. That is the
# double-count guard: without it, two spans report the same tokens and the backend bills twice.
# Every function here does both, which is why they exist rather than a docs paragraph.

from __future__ import annotations

from typing import Any

from temporal_agent_harness.harness import tracing


def instrument_pydantic_ai(
    *,
    include_content: bool = True,
    include_binary_content: bool = False,
    tracer_provider: Any = None,
) -> Any:
    """Turn on Pydantic AI's native OTel instrumentation for every agent in this process.

    Returns the ``InstrumentationSettings`` in case a caller wants to attach them to individual
    agents instead; ``Agent.instrument_all`` has already been applied.

    ``include_content=True`` is what actually puts prompts and completions on the spans — the
    whole point of this tier. Turn it OFF for a production deployment handling real user data
    unless the backend is inside your compliance boundary: it ships message text to wherever
    your traces go.
    """
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    settings = InstrumentationSettings(
        include_content=include_content,
        include_binary_content=include_binary_content,
        **({"tracer_provider": tracer_provider} if tracer_provider is not None else {}),
    )
    Agent.instrument_all(settings)
    tracing.mark_sdk_instrumented(tracing.SDK_PYDANTIC_AI)
    return settings


def instrument_openai_agents(*, tracer_provider: Any = None) -> None:
    """Turn on OpenInference's instrumentation for the OpenAI Agents SDK.

    Needs ``openinference-instrumentation-openai-agents``, which is not a harness dependency.

    NOTE the interaction with the vendored plugin: ``OpenAIAgentsPlugin`` has its own
    ``use_otel_instrumentation=True`` mode that installs this same instrumentor and builds its
    own trace tree from the Agents SDK's spans. Use ONE of the two. If you enable the plugin's
    mode, call :func:`mark_openai_agents_instrumented` instead of this, so the harness still
    knows to stop claiming the tokens.
    """
    from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor

    OpenAIAgentsInstrumentor().instrument(
        **({"tracer_provider": tracer_provider} if tracer_provider is not None else {})
    )
    tracing.mark_sdk_instrumented(tracing.SDK_OPENAI_AGENTS)


def instrument_google_genai(*, tracer_provider: Any = None) -> None:
    """Turn on OpenInference's instrumentation for the Google GenAI SDK.

    Needs ``openinference-instrumentation-google-genai``, which is not a harness dependency.
    """
    from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor

    GoogleGenAIInstrumentor().instrument(
        **({"tracer_provider": tracer_provider} if tracer_provider is not None else {})
    )
    tracing.mark_sdk_instrumented(tracing.SDK_GOOGLE_GENAI)


def mark_openai_agents_instrumented() -> None:
    """Declare that something else already instruments the OpenAI Agents SDK here.

    For the case where the instrumentor is installed by another path — notably
    ``OpenAIAgentsPlugin(use_otel_instrumentation=True)`` — and the harness still needs to know
    not to bill the same tokens twice.
    """
    tracing.mark_sdk_instrumented(tracing.SDK_OPENAI_AGENTS)


__all__ = [
    "instrument_google_genai",
    "instrument_openai_agents",
    "instrument_pydantic_ai",
    "mark_openai_agents_instrumented",
]
