"""Worker-side activity that calls the OpenAI Decisions API."""

from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .models import (
    OPENAI_DECISIONS_APPROVAL_ACTIVITY,
    OpenAIDecisionAnswer,
    OpenAIDecisionRequest,
)

_client: Any | None = None


def _decisions_client():
    global _client
    if _client is None:
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise ApplicationError(
                "The OpenAI Decisions evaluator requires the `openai-decisions` extra. "
                "Install temporal-agent-harness[openai-decisions] on this worker and "
                "redeploy it.",
                type="OpenAIDecisionsExtraMissing",
                non_retryable=True,
            ) from e
        _client = AsyncOpenAI()
    if not hasattr(_client, "decisions"):
        raise ApplicationError(
            "The installed OpenAI Python SDK is too old for the Decisions API. Install "
            "openai>=3.26.0 (or temporal-agent-harness[openai-decisions]) on this "
            "worker and redeploy it.",
            type="OpenAIDecisionsUnsupported",
            non_retryable=True,
        )
    return _client


@activity.defn(name=OPENAI_DECISIONS_APPROVAL_ACTIVITY)
async def openai_decisions_tool_approval(
    request: OpenAIDecisionRequest,
) -> OpenAIDecisionAnswer:
    """Ask one typed Choice and one Predicate question about a gated tool call."""
    client = _decisions_client()
    try:
        decision = await client.decisions.create(
            input=request.input,
            model=request.model,
            questions=request.questions,
        )
    except AttributeError as e:
        raise ApplicationError(
            "The installed OpenAI Python SDK does not expose the Decisions API. Install "
            "openai>=3.26.0 (or temporal-agent-harness[openai-decisions]) on this "
            "worker and redeploy it.",
            type="OpenAIDecisionsUnsupported",
            non_retryable=True,
        ) from e

    answers = {answer.name: answer for answer in decision.answers}
    verdict_answer = answers.get("verdict")
    irreversible_answer = answers.get("irreversible")
    refused = (
        verdict_answer is None
        or irreversible_answer is None
        or verdict_answer.type != "choice"
        or irreversible_answer.type != "predicate"
    )

    if refused:
        # Any unexpected or refused answer must go to a person, never become an approval.
        verdict = "refusal"
        confidence = 0.0
        probabilities: dict[str, float] = {}
        irreversible = 1.0
    else:
        verdict = str(verdict_answer.choice)
        confidence = verdict_answer.confidence
        probabilities = {
            str(option.value): option.probability
            for option in verdict_answer.probabilities
        }
        irreversible = irreversible_answer.probability

    usage = decision.usage
    return OpenAIDecisionAnswer(
        model=decision.model,
        verdict=verdict,
        verdict_confidence=confidence,
        verdict_probabilities=probabilities,
        irreversible=irreversible,
        refused=refused,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )


OPENAI_DECISIONS_APPROVAL_ACTIVITIES = [openai_decisions_tool_approval]
