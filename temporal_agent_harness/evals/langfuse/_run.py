# ABOUTME: ``run_experiment`` — drive a whole Langfuse dataset through a live agent worker and
# record the scores, one experiment run.
#
# Thin by design. Langfuse's own ``dataset.run_experiment`` wants a synchronous task function,
# which does not fit an async Temporal client, so this drives the items itself and writes the
# results through the same public API. Doing it this way also keeps the loop legible — which
# matters, because the durable version of exactly this loop (a Temporal workflow fanning out
# over items so a half-finished run resumes without re-spending tokens) replaces it later.

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from temporalio.client import Client

from temporal_agent_harness.evals.langfuse._experiment import attach_scores, get_client
from temporal_agent_harness.evals.runner import run_script
from temporal_agent_harness.evals.script import ScriptResult, TurnScript
from temporal_agent_harness.evals.scoring import EvaluatorFn, Score, run_evaluators

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langfuse import Langfuse


@dataclass
class ItemOutcome:
    """One dataset item's run and its scores."""

    item_id: str
    script: TurnScript
    result: ScriptResult
    scores: list[Score] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.result.ok and all(s.is_pass for s in self.scores)


@dataclass
class ExperimentSummary:
    """The aggregate of one experiment run — what you compare between two runs."""

    run_name: str
    outcomes: list[ItemOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)

    def score_rates(self) -> dict[str, float]:
        """Mean value per score name across every item that produced that score.

        The per-check breakdown, which is what tells you *what* regressed rather than merely
        that something did.
        """
        totals: dict[str, list[float]] = {}
        for outcome in self.outcomes:
            for score in outcome.scores:
                if isinstance(score.value, float | int):
                    totals.setdefault(score.name, []).append(float(score.value))
        return {name: sum(v) / len(v) for name, v in totals.items() if v}

    def format(self) -> str:
        lines = [f"{self.run_name}: {self.passed}/{self.total} cases passed"]
        for name, rate in sorted(self.score_rates().items()):
            lines.append(f"  {name:<32} {rate:>6.0%}")
        for outcome in self.outcomes:
            if outcome.passed:
                continue
            reasons = [
                s.comment or s.name for s in outcome.scores if not s.is_pass
            ] or [outcome.result.error or "turn failed"]
            lines.append(f"  FAIL {outcome.item_id}: {'; '.join(reasons)}")
        return "\n".join(lines)


async def run_experiment(
    temporal: Client,
    dataset_name: str,
    run_name: str,
    *,
    evaluators: list[EvaluatorFn],
    max_concurrency: int = 4,
    item_ids: list[str] | None = None,
    timeout: float | None = None,
    langfuse: Langfuse | None = None,
) -> ExperimentSummary:
    """Run every item in ``dataset_name`` against its agent and record scores under ``run_name``.

    Items run concurrently up to ``max_concurrency`` — each gets its own agent session, so they
    are genuinely independent. Keep it modest: every item is a real session doing real model
    calls, and the bound is there to avoid stampeding one worker's task queue, not to be fast.

    ``item_ids`` restricts the run to specific cases — the tight loop you want while iterating
    on a single failing case rather than re-running the whole dataset.
    """
    client = langfuse or get_client()
    dataset = client.get_dataset(dataset_name)
    items = [i for i in dataset.items if item_ids is None or i.id in item_ids]
    semaphore = asyncio.Semaphore(max_concurrency)

    async def run_one(item: Any) -> ItemOutcome:
        async with semaphore:
            script = TurnScript.model_validate(item.input)
            result = await run_script(
                temporal,
                script,
                labels={
                    "dataset": dataset_name,
                    "dataset_item_id": str(item.id),
                    "run": run_name,
                },
                timeout=timeout,
            )
            scores = await run_evaluators(evaluators, script, result)
            # Scores hang off the trace the harness already emitted for the turn — no span is
            # created here.
            attach_scores(result, scores, client=client)
            _link_item(item, result, run_name)
            return ItemOutcome(
                item_id=str(item.id), script=script, result=result, scores=scores
            )

    outcomes = await asyncio.gather(*(run_one(i) for i in items))
    client.flush()
    return ExperimentSummary(run_name=run_name, outcomes=list(outcomes))


def _link_item(item: Any, result: ScriptResult, run_name: str) -> None:
    """Associate a dataset item with the run's trace, so the item shows up in the experiment.

    Best-effort: ``item.link`` is the part of the Langfuse API most likely to move between
    versions, and losing the association costs you a nicer UI view — not the scores, which are
    already attached to the trace by id.
    """
    trace_ids = [t for t in result.trace_ids if t]
    if not trace_ids:
        return
    try:
        item.link(trace_ids[-1], run_name)
    except Exception:  # noqa: BLE001,S110 — see docstring
        pass


__all__ = ["ExperimentSummary", "ItemOutcome", "run_experiment"]
