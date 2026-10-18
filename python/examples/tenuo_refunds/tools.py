"""The support agent's two tools, as durable activity tools, over a canned order book.

Both go through the approval gate, and with auto mode on, through the Tenuo evaluator: the
warrant decides which orders the agent may read and how much it may refund.
"""

from __future__ import annotations

from temporal_agent_harness.harness import agent

# A stand-in for the shop's order system. Amounts are in cents.
ORDERS: dict[str, dict] = {
    "A-1001": {"customer": "Maya Chen", "item": "Standing desk", "total_cents": 12000},
    "A-1002": {"customer": "Jordan Lee", "item": "Monitor arm", "total_cents": 18900},
}


@agent.activity_tool_defn()
async def get_order(order_id: str) -> dict:
    """Look up an order by ID, e.g. "A-1001". Returns the customer, the item and the total in
    cents."""
    order = ORDERS.get(order_id)
    if order is None:
        raise ValueError(f"no order {order_id!r}")
    return {"order_id": order_id, **order}


@agent.activity_tool_defn()
async def refund(order_id: str, amount_cents: int) -> str:
    """Refund part or all of an order to the customer's original payment method. Moves real
    money. `amount_cents` is in cents: $30.00 is 3000."""
    # Canned, like the order book: a real tool would call the payment provider here.
    return f"Refunded ${amount_cents / 100:.2f} on {order_id}."


ALL_TOOLS = [get_order, refund]
