"""Automatic model-selection decisions."""

from __future__ import annotations

from dataclasses import dataclass

from . import policy
from .classifier import Classification, classify
from .models import ModelRoutingConfig


@dataclass(frozen=True)
class RouteResult:
    model_id: str
    tier: str
    classification: Classification
    fallback_applied: bool
    stakes_override_applied: bool


def resolve_tier(
    classification: Classification, config: ModelRoutingConfig
) -> tuple[str, bool, bool]:
    fallback_applied = (
        classification.tier_confidence < policy.CONFIDENCE_FALLBACK_THRESHOLD
    )
    tier = config.fallback_tier if fallback_applied else classification.tier

    stakes_override = False
    if classification.stakes_score > policy.STAKES_OVERRIDE_THRESHOLD:
        current_index = policy.TIER_ORDER.index(tier)
        if current_index < len(policy.TIER_ORDER) - 1:
            tier = policy.TIER_ORDER[current_index + 1]
            stakes_override = True

    return tier, fallback_applied, stakes_override


def route(prompt: str, config: ModelRoutingConfig) -> RouteResult:
    classification = classify(prompt)
    tier, fallback_applied, stakes_override_applied = resolve_tier(
        classification, config
    )
    return RouteResult(
        model_id=config.tier_to_model[tier],
        tier=tier,
        classification=classification,
        fallback_applied=fallback_applied,
        stakes_override_applied=stakes_override_applied,
    )
