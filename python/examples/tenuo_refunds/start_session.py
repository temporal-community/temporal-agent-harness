"""Issue a warrant for one order and start a Tenuo Refunds session with it.

Run with `just session A-1001` (or let `just demo` run it), or from the repo root with uv:
    PYTHONPATH=python uv run --project python --group examples \
        python -m examples.tenuo_refunds.start_session A-1001

This is what a support platform would do when a customer writes in: issue a warrant scoped to
their order, then start the agent's session with it. It waits for the harness server and the
agent's worker, creates the session through the server's API (the same call the UI's
New session makes), and prints its link.

Env vars:
    HARNESS_URL   the harness server (default: http://localhost:8000)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

from examples.tenuo_refunds.issue_warrant import AUTONOMOUS_REFUND_CENTS, issue
from examples.tenuo_refunds.keys import confirm_in_chat
from examples.tenuo_refunds.tools import ORDERS

AGENT = "TenuoRefundsAgent"
# The first `just demo` builds every program with Bazel before the server answers.
WAIT_SECONDS = 600


def _call(base: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        base + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _wait_for_worker(base: str) -> None:
    """Wait until the server is up and the agent's worker is polling its task queue."""
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            agents = _call(base, "/api/agents")["agents"]
            agent = next((a for a in agents if a["workflow_type"] == AGENT), None)
            if agent is None:
                sys.exit(
                    f"error: the server at {base} doesn't serve {AGENT}; "
                    "start it with this example's `just server`"
                )
            if agent["worker"]["poller_count"] > 0:
                return
        except (urllib.error.URLError, ConnectionError):
            pass
        if time.monotonic() > deadline:
            sys.exit(
                f"error: no {AGENT} worker at {base} after {WAIT_SECONDS}s; "
                "is `just worker` running?"
            )
        time.sleep(2)


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ORDERS:
        sys.exit(f"usage: start_session.py <order_id>   (one of: {', '.join(ORDERS)})")
    order_id = sys.argv[1]
    base = os.environ.get("HARNESS_URL", "http://localhost:8000").rstrip("/")

    confirm = confirm_in_chat()
    warrant = issue(order_id, signed_approvals=not confirm)
    _wait_for_worker(base)
    body = {"agent_workflow_type": AGENT, "data": {"warrant": warrant.to_base64()}}
    session = _call(base, "/api/sessions", body)

    order = ORDERS[order_id]
    total = order["total_cents"] / 100
    limit = AUTONOMOUS_REFUND_CENTS / 100
    item = order["item"].lower()
    other = next(o for o in ORDERS if o != order_id)
    if confirm:
        rules = f"""  - read order {order_id}, and no other
  - refund up to ${total:.2f} on it

and the worker's confirm rule asks in the chat before any refund over ${limit:.2f}."""
        tries = f"""  1. "The {item} on order {order_id} arrived scratched, can I get $30 back?"
     Runs straight away.
  2. "It's damaged worse than I thought. Could you refund another $60?"
     Waits for someone to approve or reject it in the chat. No keys: anyone with the chat
     can decide it, inside what the warrant allows.
  3. "Please refund ${total + 50:.2f} on order {order_id}."
     Denied: more than the warrant allows, so nobody is asked.
"""
    else:
        rules = f"""  - read order {order_id}, and no other
  - refund up to ${total:.2f} on it
  - refund up to ${limit:.2f} on its own; anything more waits for the support lead"""
        tries = f"""  1. "The {item} on order {order_id} arrived scratched, can I get $30 back?"
     Runs straight away.
  2. "It's damaged worse than I thought. Could you refund another $60?"
     Waits for the support lead. The worker's output asks you: type y and press Enter.
     The chat also shows Approve and Reject buttons for it. Don't use them: they're the
     harness's own, and they skip the support lead.
  3. "Please also refund $20 on order {other}."
     Denied: the warrant is for {order_id} only.
"""
    print(
        f"""
Session started for order {order_id} ({order["item"]}, ${total:.2f}).

Its warrant lets the agent, for the next hour:
{rules}

Open it:  {base}/?s={session["workflow_id"]}

Then try:
{tries}""",
        flush=True,
    )


if __name__ == "__main__":
    main()
