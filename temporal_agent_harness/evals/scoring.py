# ABOUTME: ``Score`` and the ``Evaluator`` protocol — the provider-neutral scoring vocabulary.
#
# Kept deliberately thinner than any provider's own scoring type so that an evaluator someone
# writes is a plain function of (script, result) with no vendor import in it. The Langfuse
# adapter converts these to its ``Evaluation``; a different backend would convert them to
# something else. An evaluator is the part a user actually writes, so it is the part most worth
# keeping portable.

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from temporal_agent_harness.evals.script import ScriptResult, TurnScript


@dataclass(frozen=True)
class Score:
    """One named judgement about one run.

    ``value`` is numeric or categorical. For a boolean check use ``1.0``/``0.0`` rather than a
    string — numeric scores aggregate into a pass rate across a dataset, which is the number
    anyone actually looks at when comparing two runs.

    ``comment`` is where an evaluator explains itself. Always populate it on a FAILING score:
    "booked FL-SFOJFK-002 at $612 when FL-SFOJFK-004 at $198 was available" turns a red cell in
    a results table into an actionable bug report, and costs one f-string.
    """

    name: str
    value: float | str
    comment: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def passed(cls, name: str, comment: str | None = None, **metadata: Any) -> Score:
        return cls(name=name, value=1.0, comment=comment, metadata=metadata)

    @classmethod
    def failed(cls, name: str, comment: str, **metadata: Any) -> Score:
        return cls(name=name, value=0.0, comment=comment, metadata=metadata)

    @classmethod
    def boolean(
        cls, name: str, ok: bool, comment: str | None = None, **metadata: Any
    ) -> Score:
        return cls(name=name, value=1.0 if ok else 0.0, comment=comment, metadata=metadata)

    @property
    def is_pass(self) -> bool:
        """Whether a numeric score is a full pass. Categorical scores are never a 'pass'."""
        return isinstance(self.value, float | int) and float(self.value) >= 1.0


@runtime_checkable
class Evaluator(Protocol):
    """Score one run. May be sync or async (async so a judge can call a model).

    Takes the whole :class:`ScriptResult`, not just the final text, because the interesting
    questions about an agent are about what it DID: which tools it called, in what order, how
    many times. Returning a list lets one evaluator emit several related scores from a single
    pass over the events.
    """

    def __call__(
        self, script: TurnScript, result: ScriptResult
    ) -> list[Score] | Awaitable[list[Score]]: ...


EvaluatorFn = Callable[[TurnScript, ScriptResult], Any]


async def run_evaluators(
    evaluators: list[EvaluatorFn], script: TurnScript, result: ScriptResult
) -> list[Score]:
    """Apply every evaluator, tolerating both sync and async ones.

    An evaluator that raises yields a failing score named after it rather than aborting the
    run: a buggy scorer should cost you one cell in the results table, not the whole dataset.
    """
    import inspect

    scores: list[Score] = []
    for evaluator in evaluators:
        name = getattr(evaluator, "__name__", repr(evaluator))
        try:
            produced = evaluator(script, result)
            if inspect.isawaitable(produced):
                produced = await produced
            scores.extend(produced or [])
        except Exception as e:  # noqa: BLE001 — see docstring
            scores.append(
                Score.failed(name, comment=f"evaluator raised: {type(e).__name__}: {e}")
            )
    return scores


__all__ = ["Evaluator", "EvaluatorFn", "Score", "run_evaluators"]
