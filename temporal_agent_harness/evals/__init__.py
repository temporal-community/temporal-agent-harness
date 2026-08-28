# ABOUTME: Public surface of the harness's evals/observability integration — tracing setup, the
# eval-case shape (TurnScript), the runner that replays one case against a fresh agent session,
# and the provider-neutral scoring vocabulary.
#
# The harness itself carries the instrumentation (``harness/tracing.py``, wired into the turn
# loop and the tool/model paths). This package is the opinionated layer on top: how to build a
# replay-safe tracer provider, where to send the spans, how to describe a dataset case, and how
# to score one.
#
# The Langfuse-specific parts live in ``temporal_agent_harness.evals.langfuse`` and are imported
# separately, so nothing here requires the Langfuse SDK.
#
# Requires the ``evals`` extra:  uv add "temporal-agent-harness[evals]"

from temporal_agent_harness.evals.runner import run_script
from temporal_agent_harness.evals.tier2 import (
    instrument_google_genai,
    instrument_openai_agents,
    instrument_pydantic_ai,
    mark_openai_agents_instrumented,
)
from temporal_agent_harness.evals.scoring import (
    Evaluator,
    EvaluatorFn,
    Score,
    run_evaluators,
)
from temporal_agent_harness.evals.script import ScriptResult, TurnScript, TurnStep
from temporal_agent_harness.evals.tracing_setup import (
    DEFAULT_LANGFUSE_HOST,
    langfuse_headers,
    langfuse_span_processor,
    setup_tracing,
)

__all__ = [
    "DEFAULT_LANGFUSE_HOST",
    "Evaluator",
    "EvaluatorFn",
    "Score",
    "ScriptResult",
    "TurnScript",
    "TurnStep",
    "instrument_google_genai",
    "instrument_openai_agents",
    "instrument_pydantic_ai",
    "langfuse_headers",
    "mark_openai_agents_instrumented",
    "langfuse_span_processor",
    "run_evaluators",
    "run_script",
    "setup_tracing",
]
