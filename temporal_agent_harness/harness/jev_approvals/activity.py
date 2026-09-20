"""The worker-side activity that asks Jev about one gated tool call.

A thin Temporal wrapper: it checks that the optional ``jev`` extra is installed, then makes
one TypeSafe System One call with the state and questions the workflow already composed
(see :mod:`.approver`) and projects the answers into :class:`JevApprovalAnswer`.

This module is WORKER-SIDE — never import it from workflow code or from ``harness.agent``.
The workflow-side approver dispatches it by NAME (``JEV_TOOL_APPROVAL_ACTIVITY``), so
nothing on the workflow path imports this module or the TypeSafe SDK.

Why the extra is checked HERE, per call, rather than at import: this module deliberately
does not import ``typesafe_sdk`` at module scope, so it loads with or without the ``jev``
extra and ``AgentHarnessPlugin`` can register the activity on every worker. That matters
because the approver dispatches by name — with nothing registered under this name Temporal
answers ``ApplicationError(type="NotFoundError")``, which is RETRYABLE, so a worker missing
the extra would retry "is not registered on this worker" forever and every gated call would
hang mid-approval. Checking inside the activity turns that into one immediate,
non-retryable failure naming the extra to install — which the approver then reports as an
ESCALATE, so the human gate still resolves the call. The check cannot live in the workflow
instead: whether a package is installed is a property of the worker process, not of
workflow history, so branching on it in-workflow would be nondeterministic across a replay.

NB: no ``from __future__ import annotations`` — the activity's annotations are read
concretely to type its result across the pydantic converter.
"""

import os

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .models import JEV_TOOL_APPROVAL_ACTIVITY, JevApprovalAnswer, JevApprovalRequest

# The error type an operator (or the approver's own error handling) can match on to tell a
# worker missing the `jev` extra apart from TypeSafe genuinely refusing the call.
JEV_MISSING_EXTRA_ERROR = "JevExtraMissing"

# Built lazily inside the worker process (a client cannot cross the activity boundary) and
# reused for the worker's lifetime. Typed loosely because the SDK is an optional import.
_client: object | None = None


def typesafe_api_key() -> str | None:
    """The TypeSafe API key from the environment.

    ``TYPESAFE_API_KEY`` is the canonical name; ``TYPESAFE_AI_API_KEY`` is accepted as an
    alias. Returning ``None`` is not an error here — the SDK client has its own resolution
    order, and letting it raise keeps one authority on what counts as configured.
    """
    return os.environ.get("TYPESAFE_API_KEY") or os.environ.get("TYPESAFE_AI_API_KEY")


def _require_jev_extra() -> None:
    """Fail the call with an actionable error unless the ``jev`` extra is installed.

    Probing ``typesafe_sdk`` directly, rather than wrapping the real import in ``except
    ImportError``, keeps the diagnosis honest: only the SDK's actual absence produces this
    error, while an ImportError from a bug elsewhere still surfaces as itself.

    Non-retryable on purpose: the call fails once, the approver turns that into an escalate,
    and a human resolves the gate — rather than the turn stalling behind a retry loop that
    cannot succeed until the worker is redeployed.
    """
    try:
        import typesafe_sdk  # noqa: F401  (presence probe only)
    except ImportError as e:
        raise ApplicationError(
            "A Jev auto-approver ran on a worker without the harness's `jev` extra, which "
            "provides typesafe-sdk — the client that asks Jev to judge a gated tool call. "
            "Install temporal-agent-harness[jev] on this worker and redeploy it.",
            type=JEV_MISSING_EXTRA_ERROR,
            non_retryable=True,
        ) from e


def _typesafe_client():
    global _client
    if _client is None:
        from typesafe_sdk import AsyncTypeSafeClient

        _client = AsyncTypeSafeClient(api_key=typesafe_api_key())
    return _client


@activity.defn(name=JEV_TOOL_APPROVAL_ACTIVITY)
async def jev_tool_approval(request: JevApprovalRequest) -> JevApprovalAnswer:
    """Ask Jev the approval questions the workflow composed about one gated tool call.

    One request, every question answered in parallel over the same state. Returns the raw
    typed judgments; the workflow composes them into a verdict.
    """
    _require_jev_extra()
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


# Registered unconditionally by ``AgentHarnessPlugin`` — the extra is checked per call by
# _require_jev_extra(), not at registration, so a worker without it fails a Jev approval
# once and escalates, instead of leaving the activity name unregistered (which Temporal
# retries forever). Use this list directly only when assembling a worker's activities by
# hand::
#
#     Worker(client, task_queue=..., activities=[*JEV_APPROVAL_ACTIVITIES, ...])
JEV_APPROVAL_ACTIVITIES = [jev_tool_approval]
