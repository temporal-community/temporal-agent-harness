"""OpenAI Decisions API evaluator for tool approval auto mode.

The workflow-side code builds a typed Decisions request and dispatches it by activity
name. Only the worker-side activity imports the optional OpenAI Python SDK, so importing
the evaluator in workflow code does not require the ``openai-decisions`` extra.
"""

from .approver import (
    DEFAULT_ACTIVITY_CONFIG,
    DEFAULT_OPENAI_DECISIONS_MODEL,
    build_request,
    decide,
    openai_decisions_evaluator,
)
from .models import (
    OPENAI_DECISIONS_APPROVAL_ACTIVITY,
    OpenAIDecisionAnswer,
    OpenAIDecisionRequest,
)

__all__ = [
    "DEFAULT_ACTIVITY_CONFIG",
    "DEFAULT_OPENAI_DECISIONS_MODEL",
    "OPENAI_DECISIONS_APPROVAL_ACTIVITY",
    "OpenAIDecisionAnswer",
    "OpenAIDecisionRequest",
    "build_request",
    "decide",
    "openai_decisions_evaluator",
]
