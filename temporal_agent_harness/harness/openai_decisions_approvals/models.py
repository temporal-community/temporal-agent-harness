"""Request and answer payloads for one OpenAI Decisions API approval activity."""

from typing import Any

from pydantic import BaseModel, Field

OPENAI_DECISIONS_APPROVAL_ACTIVITY = "openai_decisions_tool_approval"
DEFAULT_OPENAI_DECISIONS_MODEL = "gpt-6-luna"


class OpenAIDecisionRequest(BaseModel):
    """Plain JSON input and typed questions, composed on the workflow side."""

    input: str
    questions: list[dict[str, Any]]
    model: str = DEFAULT_OPENAI_DECISIONS_MODEL


class OpenAIDecisionAnswer(BaseModel):
    """Raw typed answers from the API; thresholds are applied workflow-side."""

    model: str
    verdict: str
    verdict_confidence: float
    verdict_probabilities: dict[str, float] = Field(default_factory=dict)
    irreversible: float
    refused: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
