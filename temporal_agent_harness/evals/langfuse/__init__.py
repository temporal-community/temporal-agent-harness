# ABOUTME: Langfuse adapter for the harness evals package — datasets, experiments, and scores.
# Trace export is NOT here: it goes over plain OTLP from the harness's own instrumentation, so
# nothing in this package creates a span.
#
# Requires the ``evals`` extra:  uv add "temporal-agent-harness[evals]"

from temporal_agent_harness.evals.langfuse._experiment import (
    attach_scores,
    get_client,
    seed_dataset,
    to_evaluation,
)
from temporal_agent_harness.evals.langfuse._run import (
    ExperimentSummary,
    ItemOutcome,
    run_experiment,
)

__all__ = [
    "ExperimentSummary",
    "ItemOutcome",
    "attach_scores",
    "get_client",
    "run_experiment",
    "seed_dataset",
    "to_evaluation",
]
