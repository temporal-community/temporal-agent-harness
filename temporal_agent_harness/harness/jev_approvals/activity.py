"""Worker-side compatibility for pre-migration Jev approvals.

New evaluations use TypeSafePlugin's canonical activity. The original activity remains
registered so pre-migration histories and already scheduled approval tasks can finish.
The canonical-name fallback supplies a non-retryable configuration error when a worker
omits TypeSafePlugin, leaving the approval for a human.

NB: no ``from __future__ import annotations`` — the activity's annotations are read
concretely to type its result across the pydantic converter.
"""

import os

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .models import (
    JEV_TOOL_APPROVAL_ACTIVITY,
    TYPESAFE_SYSTEM_ONE_ACTIVITY,
    JevApprovalAnswer,
    JevApprovalRequest,
)

TYPESAFE_MISSING_PLUGIN_ERROR = "TypeSafePluginMissing"


@activity.defn(name=TYPESAFE_SYSTEM_ONE_ACTIVITY)
async def missing_typesafe_plugin(payload: dict) -> dict:
    """Fail once when no canonical provider activity was registered before the harness."""
    raise ApplicationError(
        "TypeSafe decisions and Jev auto-approval require TypeSafePlugin. Register "
        "temporalio.typesafe.TypeSafePlugin with an AsyncTypeSafeClient configured with "
        "RetryPolicy(max_retries=0), before AgentHarnessPlugin on this worker.",
        type=TYPESAFE_MISSING_PLUGIN_ERROR,
        non_retryable=True,
    )


# Built lazily inside the worker process (a client cannot cross the activity boundary) and
# reused only by the legacy compatibility activity.
_client: object | None = None


def typesafe_api_key() -> str | None:
    """The TypeSafe API key from the environment.

    ``TYPESAFE_API_KEY`` is the canonical name; ``TYPESAFE_AI_API_KEY`` is accepted as an
    alias. Returning ``None`` is not an error here — the SDK client has its own resolution
    order, and letting it raise keeps one authority on what counts as configured.
    """
    return os.environ.get("TYPESAFE_API_KEY") or os.environ.get("TYPESAFE_AI_API_KEY")


def _typesafe_client():
    global _client
    if _client is None:
        from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

        _client = AsyncTypeSafeClient(
            api_key=typesafe_api_key(), retry=RetryPolicy(max_retries=0),
        )
    return _client


@activity.defn(name=JEV_TOOL_APPROVAL_ACTIVITY)
async def jev_tool_approval(request: JevApprovalRequest) -> JevApprovalAnswer:
    """Ask Jev the approval questions the workflow composed about one gated tool call.

    One request, every question answered in parallel over the same state. Returns the raw
    typed judgments; the workflow composes them into a verdict.
    """
    response = await _typesafe_client().system_one(
        request.state,
        request.questions,  # raw question dicts are accepted by the SDK
        model=request.model,
    )
    verdict = response.choices["verdict"]
    return JevApprovalAnswer(
        model=response.model,
        request_id=response.request_id,
        verdict=verdict.choice,
        verdict_confidence=verdict.confidence,
        verdict_probabilities=dict(verdict.probabilities),
        irreversible=response.nouls["irreversible"].noul,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


# Registered by AgentHarnessPlugin for pre-migration histories. New evaluations require
# TypeSafePlugin, not this compatibility activity. For a manually assembled legacy worker::
#
#     Worker(client, task_queue=..., activities=[*JEV_APPROVAL_ACTIVITIES, ...])
JEV_APPROVAL_ACTIVITIES = [jev_tool_approval]
