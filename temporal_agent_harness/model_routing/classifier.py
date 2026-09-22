"""Request classification for automatic model selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import policy

if TYPE_CHECKING:
    from typesafe_sdk import TypeSafeClient


@dataclass(frozen=True)
class Classification:
    tier: str
    tier_confidence: float
    stakes_score: float
    stakes_confidence: float


def classify(prompt: str, client: TypeSafeClient | None = None) -> Classification:
    from typesafe_sdk import Choice, Score, TypeSafeClient

    owns_client = client is None
    client = client or TypeSafeClient()
    try:
        response = client.system_one(
            state=prompt,
            questions={
                "tier": Choice(
                    instructions=policy.TIER_INSTRUCTIONS,
                    criteria=policy.TIER_CRITERIA,
                ),
                "stakes": Score(
                    instructions=policy.STAKES_INSTRUCTIONS,
                    criteria=policy.STAKES_CRITERIA,
                ),
            },
        )
    finally:
        if owns_client:
            client.close()

    tier_answer = response.answers["tier"]
    stakes_answer = response.answers["stakes"]
    return Classification(
        tier=tier_answer.choice,
        tier_confidence=tier_answer.confidence,
        stakes_score=stakes_answer.score,
        stakes_confidence=stakes_answer.confidence,
    )
