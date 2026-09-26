"""Automatic model-selection policy."""

DEFAULT_TIER = "moderate"
TIER_ORDER = ("simple", "moderate", "complex", "frontier")

TIER_CRITERIA = {
    "simple": (
        "Greetings, small talk, basic factual lookups, formatting/rewriting requests, "
        "or anything with a single obvious correct answer."
    ),
    "moderate": (
        "Typical reasoning, multi-step tasks, everyday coding, or questions that need "
        "some judgment but aren't ambiguous or high-stakes."
    ),
    "complex": (
        "Ambiguous, open-ended, high-stakes, or safety/compliance-sensitive requests, "
        "or anything requiring deep multi-step reasoning where mistakes are costly."
    ),
    "frontier": (
        "Exceptional work that needs unusually broad context, extended autonomous execution, "
        "or difficult synthesis across many interdependent parts."
    ),
}

TIER_INSTRUCTIONS = (
    "What kind of request is this, in terms of how much reasoning capability it needs?"
)
STAKES_INSTRUCTIONS = "How costly would a wrong or low-quality answer to this request be?"
STAKES_CRITERIA = [
    "No real consequence - easily corrected or ignored",
    "Some cost to get wrong - wasted time, minor rework",
    "Seriously costly - financial, legal, safety, or irreversible impact",
]

CONFIDENCE_FALLBACK_THRESHOLD = 0.7
STAKES_OVERRIDE_THRESHOLD = 1.5
