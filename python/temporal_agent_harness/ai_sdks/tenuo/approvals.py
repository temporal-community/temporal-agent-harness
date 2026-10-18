"""Signed approvals for calls a Tenuo warrant gates, through Tenuo's own approval handlers.

When a warrant says a call needs approval, it also names who may approve it (the approvers'
public keys) and how many must. ``tenuo_evaluator`` gets those approvals the same way Tenuo's
Temporal plugin does: it calls the ``approval_handler`` on the worker's ``TenuoPluginConfig``,
in an activity, and then verifies what comes back against the warrant. Any Tenuo approval
handler works:

  * ``tenuo.approval.cli_prompt(approver_key=...)`` asks in the worker's terminal and signs with
    the approver's key, for local development;
  * Tenuo Cloud's handlers send the request to the dashboard or Slack, where approvers sign. With
    ``from tenuo_cloud.temporal import TenuoPluginConfig`` and ``TENUO_API_KEY`` set, the config
    wires one up on its own.

A handler that is still waiting raises a retryable ``ApplicationError``, and Temporal retries the
activity, so no worker slot is held while a person decides. The evaluator bounds the retries by
the warrant's expiry.

Register :meth:`TenuoApprovalActivities.all` on the agent's worker, built from the same config
the evaluator uses.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from tenuo import Warrant
    from tenuo.approval import ApprovalDenied, ApprovalRequest, ApprovalTimeout
    from tenuo.temporal import TenuoPluginConfig
    from tenuo_core import py_compute_request_hash

#: The activity the evaluator dispatches; :class:`TenuoApprovalActivities` registers it.
REQUEST_APPROVAL_ACTIVITY = "tenuo_request_approval"


class ApprovalInput(BaseModel):
    """One call to approve, under one warrant."""

    warrant: str  # the leaf warrant, base64
    tool: str
    arguments: dict[str, Any]


class ApprovalOutcome(BaseModel):
    """Signed approvals (base64), or why there won't be any."""

    approvals: list[str] = []
    denied: str | None = None


def approval_request(warrant: Warrant, tool: str, arguments: dict[str, Any]) -> ApprovalRequest:
    """The request approvers sign for this exact call, built as Tenuo's Temporal plugin builds it.
    Its hash binds the warrant, the tool, the arguments and the holder, so an approval can't be
    reused for a different call."""
    holder_key = warrant.holder_key
    request_hash = py_compute_request_hash(warrant.id, tool, arguments, holder_key)
    return ApprovalRequest.for_warrant_gate(
        tool, arguments, warrant, request_hash, holder_key=holder_key
    )


class TenuoApprovalActivities:
    """The approval activity, bound to the worker's ``TenuoPluginConfig``. Register :meth:`all`."""

    def __init__(self, config: TenuoPluginConfig) -> None:
        self._config = config

    @activity.defn(name=REQUEST_APPROVAL_ACTIVITY)
    async def request_approval(self, req: ApprovalInput) -> ApprovalOutcome:
        handler = self._config.approval_handler
        if handler is None:
            return ApprovalOutcome(
                denied="no approval_handler is set on the worker's TenuoPluginConfig"
            )
        request = approval_request(Warrant.from_base64(req.warrant), req.tool, req.arguments)
        try:
            if inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
                getattr(handler, "__call__", None)
            ):
                result = await handler(request)
            else:
                # A synchronous handler (cli_prompt waits on the terminal) mustn't block the
                # worker's event loop.
                result = await asyncio.to_thread(handler, request)
                if inspect.isawaitable(result):
                    result = await result
        except (ApprovalDenied, ApprovalTimeout) as e:
            return ApprovalOutcome(denied=str(e) or type(e).__name__)
        # A retryable ApplicationError means "still waiting": it propagates, and Temporal
        # retries this activity. Anything else fails the activity and the evaluator denies.
        collected = result if isinstance(result, list) else [result]
        return ApprovalOutcome(
            approvals=[base64.b64encode(a.to_bytes()).decode("ascii") for a in collected]
        )

    def all(self) -> list[Callable[..., Any]]:
        return [self.request_approval]
