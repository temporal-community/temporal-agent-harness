"""Issue a warrant for one refund request, the way a support platform would.

`just session A-1001` issues one and starts a session with it (see ``start_session.py``). To
print the warrant on its own, run from the repo root with uv:
    PYTHONPATH=python uv run --project python --group examples python -m examples.tenuo_refunds.issue_warrant A-1001

It looks the order up and issues a warrant that lets the agent, for the next hour:
  * read that order and no other;
  * refund up to the order's total, on that order only;
  * refund up to $50 on its own. Anything above needs the support lead's signed approval,
    which the worker asks for in its terminal.

Run on its own, it prints the warrant in base64, the form a session's init data carries it in.
"""

from __future__ import annotations

import sys

from tenuo import Exact, Range, Warrant

from examples.tenuo_refunds import keys
from examples.tenuo_refunds.tools import ORDERS

#: Refunds up to this many cents run without asking anyone.
AUTONOMOUS_REFUND_CENTS = 5000
TTL_SECONDS = 3600


def issue(order_id: str, *, signed_approvals: bool = True) -> Warrant:
    """The warrant for one order. With ``signed_approvals``, refunds over
    ``AUTONOMOUS_REFUND_CENTS`` need the support lead's signature; without, the warrant only
    sets the ceiling, and a person confirms in the harness UI (``just demo-confirm``)."""
    order = ORDERS[order_id]
    builder = (
        Warrant.mint_builder()
        .capability("get_order", order_id=Exact(order_id))
        .capability(
            "refund",
            order_id=Exact(order_id),
            amount_cents=Range(1, order["total_cents"]),
        )
    )
    if signed_approvals:
        builder = (
            builder.approval_gates(
                {"refund": {"amount_cents": {"exempt": Range(1, AUTONOMOUS_REFUND_CENTS)}}}
            )
            # Who may approve what the gate holds back, and how many of them must.
            .required_approvers([keys.approver().public_key])
            .min_approvals(1)
        )
    return builder.holder(keys.holder().public_key).ttl(TTL_SECONDS).mint(keys.root())


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ORDERS:
        sys.exit(f"usage: issue_warrant.py <order_id>   (one of: {', '.join(ORDERS)})")
    order_id = sys.argv[1]
    total = ORDERS[order_id]["total_cents"] / 100
    print(
        f"# Warrant for {order_id}: read it, refund up to ${total:.2f}, "
        f"anything over ${AUTONOMOUS_REFUND_CENTS / 100:.2f} needs the support lead's approval. "
        "Valid 1 hour.",
        file=sys.stderr,
    )
    print(issue(order_id).to_base64())


if __name__ == "__main__":
    main()
