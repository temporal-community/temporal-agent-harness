"""Pydantic models that cross the Temporal converter: the accepted messages and the
request/result of the TypeSafe activity.

Kept apart from ``activities.py`` (which imports the TypeSafe SDK) so the message and
activity payload shapes are importable anywhere without the SDK.

NB: no ``from __future__ import annotations`` — stringized annotations trip the pydantic
converter and the handler-parameter schemas the UI renders forms from.
"""

from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Accepted messages
# ---------------------------------------------------------------------------


class NewGame(BaseModel):
    """Start a fresh game."""

    agent_goes_first: bool = False
    """If true the agent plays X and opens; otherwise you are X and open."""


class PlayMove(BaseModel):
    """Your move: the number of an empty cell."""

    cell: int = Field(ge=1, le=9, description="1-9, reading left-to-right, top-to-bottom.")


# ---------------------------------------------------------------------------
# The TypeSafe activity's request / result
# ---------------------------------------------------------------------------


class SystemOneRequest(BaseModel):
    state: dict[str, Any]
    """The JSON state the model judges — the board, whose turn, the candidate cells."""
    questions: dict[str, dict[str, Any]]
    """Raw TypeSafe question dicts keyed by question id (``type`` is ``noul``/``choice``/``score``)."""
    model: str | None = None
    """Model override; ``None`` takes the client default (``TYPESAFE_DEFAULT_MODEL`` / ``jev-latest``)."""


class ChoiceResult(BaseModel):
    choice: str
    confidence: float
    probabilities: dict[str, float]


class ScoreResult(BaseModel):
    score: float
    confidence: float
    probabilities: dict[int, float]
    legend: dict[int, Any]


class SystemOneResult(BaseModel):
    model: str
    """The model that actually answered (the API's resolved id)."""
    request_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    nouls: dict[str, float] = {}
    """Question id → probability of yes."""
    choices: dict[str, ChoiceResult] = {}
    scores: dict[str, ScoreResult] = {}

    def __str__(self) -> str:
        # The harness publishes ``str(result)`` as a tool's ``tool_output``; JSON reads better
        # in the console than pydantic's default repr.
        return self.model_dump_json(indent=2)
