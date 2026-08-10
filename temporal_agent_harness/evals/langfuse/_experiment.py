# ABOUTME: The Langfuse half of an eval run — seed a dataset from TurnScripts, run the dataset
# against a live agent worker, and attach the resulting Scores to the traces the harness
# already emitted.
#
# Note what is NOT here: any span creation. The traces already exist, produced in-process by
# the harness's own instrumentation (``harness/tracing.py``) and shipped over OTLP. This module
# only writes the things OpenTelemetry has no vocabulary for — datasets, experiments, and
# scores — and links them to those traces by ``otel_trace_id``, which every turn reports on its
# ``TurnStarted`` event and on ``TurnResult``.
#
# That split is the whole reason the integration is small: tracing is a solved problem the
# moment you have replay-safe OTel, so the provider SDK is left with only the eval-specific
# concepts.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from temporal_agent_harness.evals.script import ScriptResult, TurnScript
from temporal_agent_harness.evals.scoring import Score

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langfuse import Langfuse


def get_client() -> Langfuse:
    """The Langfuse client, from ``LANGFUSE_*`` env vars."""
    from langfuse import get_client as _get_client

    return _get_client()


def seed_dataset(
    dataset_name: str,
    cases: dict[str, TurnScript],
    *,
    description: str | None = None,
    client: Langfuse | None = None,
) -> None:
    """Create/refresh ``dataset_name`` from ``{case_id: TurnScript}``.

    Items are keyed by a caller-chosen stable id (Langfuse upserts on it), so re-seeding an
    edited dataset updates the cases in place instead of duplicating them — which matters
    because an experiment's history is only comparable if the item ids stay put across runs.

    The item's ``input`` is the serialized ``TurnScript``, so a run is fully reproducible from
    the dataset alone: it names the agent, the task queue, the config, and every message.
    """
    client = client or get_client()
    client.create_dataset(name=dataset_name, description=description)
    for case_id, script in cases.items():
        client.create_dataset_item(
            dataset_name=dataset_name,
            id=case_id,
            input=script.model_dump(mode="json"),
            expected_output=script.expected,
        )
    client.flush()


def to_evaluation(score: Score) -> Any:
    """Convert a harness :class:`Score` to a Langfuse ``Evaluation``."""
    from langfuse import Evaluation

    return Evaluation(
        name=score.name,
        value=score.value,
        comment=score.comment,
        metadata=score.metadata or None,
    )


def attach_scores(
    result: ScriptResult,
    scores: list[Score],
    *,
    client: Langfuse | None = None,
) -> int:
    """Attach ``scores`` to the trace of the run's LAST turn. Returns how many were written.

    The last turn is the one whose reply the scores are about; the earlier turns of a
    conversation are already reachable from it through the shared Langfuse session.

    Silently writes nothing when the run has no trace id — that is the expected state when
    tracing is not configured, and an eval run without traces is still a perfectly valid eval
    run. It should not fail, and it should not pretend it wrote something.
    """
    trace_ids = [t for t in result.trace_ids if t]
    if not trace_ids or not scores:
        return 0
    client = client or get_client()
    for score in scores:
        client.create_score(
            trace_id=trace_ids[-1],
            name=score.name,
            value=score.value,
            comment=score.comment,
        )
    client.flush()
    return len(scores)


__all__ = ["attach_scores", "get_client", "seed_dataset", "to_evaluation"]
