"""Approval questions, projected answers and provider activity names.

These models remain independent of worker-side provider I/O and credentials.

NB: no ``from __future__ import annotations`` — these models cross Temporal's pydantic
converter, which builds their ``TypeAdapter`` inside the workflow sandbox, where a
stringized annotation fails to resolve.
"""

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from temporalio.typesafe.workflow import SystemOneResult

# Retained for histories recorded before the canonical TypeSafe integration.
JEV_TOOL_APPROVAL_ACTIVITY = "jev_tool_approval"

# The canonical provider activity, also used by the missing-plugin fallback.
TYPESAFE_SYSTEM_ONE_ACTIVITY = "temporalio.typesafe.system_one"

# The default model. TypeSafe resolves ``jev-latest`` to a concrete release; the answer's
# ``model`` reports which one actually judged the call, so an audit of an approval decision
# can name the exact model that made it.
DEFAULT_JEV_MODEL = "jev-latest"


class JevApprovalRequest(BaseModel):
    """One System One call: the state describing a gated tool call, and the questions to
    ask about it.

    Deliberately generic — ``state`` and ``questions`` are built workflow-side by
    :func:`~.approver.jev_evaluator` and ferried verbatim. That split is what puts the
    EXACT prompt that decided an approval into workflow history: the activity's input is
    the whole question as asked, so "why was this approved" is answerable from the event
    history alone, without re-deriving anything.
    """

    state: dict[str, Any]
    """The JSON state Jev judges — the tool, its arguments, and the operator's policy."""
    questions: dict[str, dict[str, Any]]
    """Raw TypeSafe question dicts keyed by question id (``type`` is ``choice``/``noul``)."""
    model: str | None = None
    """Model override; ``None`` takes the client default (``TYPESAFE_DEFAULT_MODEL``)."""


class JevApprovalAnswer(BaseModel):
    """Jev's typed answers about one gated tool call.

    Raw judgments only: this carries what the model said, never what the harness decided
    from it. Composing these numbers into an approve/deny/escalate verdict is code's job
    (see :func:`~.approver.decide`), so the thresholds can change — or be audited against a
    past decision — without re-running inference.
    """

    model: str
    """The model that actually answered (the API's resolved id)."""
    request_id: str | None = None
    """TypeSafe's id for the call, when reported by the provider."""
    verdict: str
    """The highest-probability label of the ``verdict`` Choice: approve / deny / escalate."""
    verdict_confidence: float
    """How concentrated the ``verdict`` distribution is (0-1)."""
    verdict_probabilities: dict[str, float] = Field(default_factory=dict)
    """The full distribution over the three verdicts, which sums to 1."""
    irreversible: float
    """Probability that the call's effect could not be undone if it turned out to be wrong."""
    input_tokens: int | None = None
    output_tokens: int | None = None

    @classmethod
    def from_system_one(cls, result: "SystemOneResult") -> "JevApprovalAnswer":
        """Project the canonical plugin's native result onto approval fields."""
        response = result.response
        verdict = response.choices["verdict"]
        irreversible = response.nouls["irreversible"]
        usage = response.usage
        return cls(
            model=response.model,
            request_id=result.request_id,
            verdict=verdict.choice,
            verdict_confidence=verdict.confidence,
            verdict_probabilities=dict(verdict.probabilities),
            irreversible=irreversible.noul,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
        )
