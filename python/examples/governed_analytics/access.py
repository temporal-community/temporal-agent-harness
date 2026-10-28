"""Access to a restricted table: granted by a person for the session, until they revoke it.

The tool policy refuses every read of a ``risk`` table unless a person granted access to it in
this session and hasn't revoked it since. Access is granted two ways:

  * **The agent asks** with ``request_access``. It puts the table and the agent's reason to a
    person through ``ask_for_access``, a callback tool the console renders as a form, and the
    person grants it or declines. Either answer is the tool's result, recorded with the policy
    as ``<prefix>/request_access/result``: the answer as ``arg0`` and the table the person was
    shown as ``arg1``. Only a grant opens the table.
  * **The person grants it unasked** with the agent's ``grant_access`` message
    (``workflow.py``), at any time, mid-turn included, recorded as ``update/grant_access``.

``revoke_access`` takes it back, recorded as ``update/revoke_access``. Either way the agent
can't grant itself access: a callback's result and a message both come from outside the model.

The policy decides. ``TableAccessBoard`` is the agent's observable state showing where each
restricted table stands, which the console's Agent state panel renders as it changes.

No ``from __future__ import annotations`` here, as in the other examples' Temporal-facing
modules (stringized annotations trip the pydantic converter).
"""

from typing import Literal

from pydantic import BaseModel, Field
from temporalio import workflow

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
from temporal_agent_harness.harness.state import HarnessState, StateRef

from examples.governed_analytics.models import GRANT_FOR_THIS_SESSION, RestrictedTable

# ---------------------------------------------------------------------------
# The observable state
# ---------------------------------------------------------------------------

AccessStatus = Literal["needs access", "granted", "declined", "revoked", "never"]


class TableAccess(HarnessState):
    """Where one restricted table stands."""

    table: str
    status: AccessStatus = "needs access"
    via: str | None = None
    """How it got there: the agent's request, or your message."""
    reason: str | None = None
    """The agent's reason, when it asked."""


class TableAccessBoard(HarnessState):
    """The restricted tables, and which the agent may read right now."""

    tables: list[TableAccess] = Field(
        default_factory=lambda: [
            TableAccess(table="risk.loan_applications"),
            TableAccess(table="risk.credit_scores"),
            TableAccess(table="customer_pii.customers", status="never", via="the tool policy"),
        ]
    )


def record_access(
    board: StateRef[TableAccessBoard],
    table: str,
    status: AccessStatus,
    *,
    via: str,
    reason: str | None = None,
) -> None:
    """Show ``table``'s new status on the board."""
    with board.mutate() as draft:
        for row in draft.tables:
            if row.table == table:
                row.status = status
                row.via = via
                row.reason = reason


# ---------------------------------------------------------------------------
# Asking a person
# ---------------------------------------------------------------------------


class AccessAnswer(BaseModel):
    # Declining is an answer like granting, not a failed call: the tool policy reads the
    # answer, and only a grant opens the table.
    access: Literal["Grant for this session", "Decline"] = Field(
        description="Grant lets the agent read this table until you revoke it."
    )


class AccessRequest(BaseModel):
    """The restricted table to grant or revoke the agent's access to."""

    table: RestrictedTable = Field(description="The restricted table.")


@agent.callback_tool_defn()
async def ask_for_access(table: RestrictedTable, reason: str) -> AccessAnswer:
    """The agent asks you for read access to a restricted table for the rest of this session.
    Grant it, or decline."""
    ...


@agent.tool_defn()
async def request_access(
    table: RestrictedTable,
    reason: str,
    access: agent.Injected[StateRef[TableAccessBoard]],
    runner: agent.Injected[AgentWorkflowRunner],
) -> AccessAnswer:
    """Ask a person for read access to a restricted table when the tool policy refuses a query
    because the table needs it. `reason` says what you need it for, in a sentence the person
    can judge. Waits for their answer, `access`: "Grant for this session" or "Decline". Once
    granted, run the refused query again. A person can revoke access at any time: whenever the
    policy refuses a table for lack of access, including one you could read before, ask again.
    If the person declines, tell the user, and don't ask again for that table unless the user
    asks you to. `customer_pii` can't be opened."""
    try:
        answer = await runner.run_tool(
            str(workflow.uuid4()), ask_for_access, table=table, reason=reason
        )
    except agent.CallbackToolError:
        # The console's own Decline button answers with an error rather than a choice; it
        # means the same thing.
        answer = AccessAnswer(access="Decline")
    granted = answer.access == GRANT_FOR_THIS_SESSION
    record_access(
        access,
        table,
        "granted" if granted else "declined",
        via="the agent's request",
        reason=reason,
    )
    return answer
