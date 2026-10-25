"""The eval dispatcher's progress, published as observable state for the console.

NB: no ``from __future__ import annotations`` — a HarnessState's annotations must be real types.
"""

from typing import Literal

from pydantic import Field

from temporal_agent_harness.harness.state import HarnessState


class EvalProgress(HarnessState):
    """Where the run is: writing cases, running them, or done."""

    phase: Literal["writing", "running", "done"] = "writing"
    cases_written: int = 0
    categories: list[str] = Field(default_factory=list)
