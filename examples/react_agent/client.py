"""A terminal client for the ReAct agent: discover sessions, answer pending ask_user questions, chat.

The agent runs on a Temporal worker (imagine it in the cloud). Its weather/geo/IP/F1 tools run
server-side, but its human-in-the-loop tool, ``ask_user``, has no server-side body: when the model
needs clarification it parks the turn and publishes a ``callback_requested`` event. THIS client —
running wherever the human is — is how those questions get answered.

It talks only to the packaged harness HTTP server (``app.py``); it needs no Temporal client of its
own. It opens a session picker that lists the ReAct agent's sessions and shows which are waiting on
an ``ask_user``; from there you can:

  * open a session and answer its open questions (``POST /api/callback-result``) — including ones
    raised before this client attached: it watches the turn resume via ``GET /api/attach``;
  * chat with a session (``POST /api/chat``, Server-Sent Events), answering any ``ask_user`` that
    arises live;
  * create a new session (``POST /api/sessions``).

Only ``ask_user`` callbacks are shown/answered here: a client fulfills the callback tools it knows
how to (this one asks a human), so it filters on ``tool_name == "ask_user"`` and declines any other
pending callback (which would belong to a different fulfiller).

Run from this example's directory (with the server + workers already up — see README.md):

    just client

or from the repo root:

    uv run --group examples python -m examples.react_agent.client [--session <id>] [--server URL]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx

WORKFLOW_TYPE = "ReactAgent"
# The one callback tool this client fulfills — by prompting the human. Any other pending callback
# tool belongs to a different fulfiller, so we filter to (and only answer) this name.
HUMAN_TOOL = "ask_user"


class _Quit(Exception):
    """Raised to leave the client entirely (vs. returning to the session picker)."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _create_session(http: httpx.AsyncClient) -> str:
    resp = await http.post("/api/sessions", json={"agent_workflow_type": WORKFLOW_TYPE})
    resp.raise_for_status()
    return resp.json()["workflow_id"]


async def _status(http: httpx.AsyncClient, session_id: str) -> dict[str, Any]:
    resp = await http.get(f"/api/status/{session_id}")
    resp.raise_for_status()
    return resp.json()


def _open_questions(status: dict[str, Any]) -> list[dict[str, Any]]:
    """The session's pending ask_user callbacks (each carries tool_id, tool_input, turn_number)."""
    return [
        c for c in status.get("pending_callbacks", []) if c.get("tool_name") == HUMAN_TOOL
    ]


async def _post_callback_result(
    http: httpx.AsyncClient,
    session_id: str,
    tool_id: str,
    *,
    result: Any = None,
    error: str | None = None,
) -> None:
    body: dict[str, Any] = {"session_id": session_id, "tool_id": tool_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    resp = await http.post("/api/callback-result", json=body)
    if resp.status_code != 200:
        # e.g. 409 CallbackResultError — a malformed result does NOT consume the gate, so the
        # human can be re-prompted; here we just report it.
        print(f"  ! failed to submit answer: {resp.status_code} {resp.text}")


async def _iter_sse(resp: httpx.Response):
    """Yield ``(event_type, data_dict)`` from an SSE response stream."""
    event_type = "message"
    async for line in resp.aiter_lines():
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            raw = line[len("data:") :].strip()
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {}
            yield event_type, data
            event_type = "message"


async def _ask(prompt: str) -> str:
    """Read a line from the terminal without blocking the event loop."""
    return (await asyncio.to_thread(input, prompt)).strip()


# ---------------------------------------------------------------------------
# Fulfilling ask_user + observing a turn
# ---------------------------------------------------------------------------


async def _handle_callback_requested(
    http: httpx.AsyncClient, session_id: str, data: dict[str, Any]
) -> None:
    """Fulfill one callback_requested event. ask_user → prompt the human; anything else → decline."""
    tool_name = data.get("tool_name", "?")
    tool_id = data["tool_id"]
    tool_input = data.get("tool_input", {})
    if tool_name == HUMAN_TOOL:
        question = tool_input.get("question", "(the agent has a question)")
        print(f"\n  ? {question}")
        answer = await _ask("  your answer> ")
        await _post_callback_result(http, session_id, tool_id, result=answer)
    else:
        # Decline (rather than leave hanging) a callback this client doesn't fulfill; the turn
        # continues and the model sees a tool error.
        await _post_callback_result(
            http, session_id, tool_id, error=f"this client does not fulfill {tool_name!r}"
        )


async def _observe(
    http: httpx.AsyncClient,
    session_id: str,
    resp: httpx.Response,
    *,
    open_after: set[str] | None = None,
) -> None:
    """Render a turn's events from an SSE stream until it ends.

    ``open_after`` gates output for an *attach* stream that replays history from offset 0: suppress
    everything until a ``callback_resolved`` for one of those tool_ids is seen (an answer we just
    submitted), then render forward. ``None`` (the chat path) renders immediately.
    """
    live = open_after is None
    printed_prefix = False
    async for event_type, data in _iter_sse(resp):
        if not live:
            if event_type == "callback_resolved" and data.get("tool_id") in open_after:
                live = True
            continue
        if event_type == "reply_delta":
            if not printed_prefix:
                print("\nreact> ", end="", flush=True)
                printed_prefix = True
            print(data.get("text", ""), end="", flush=True)
        elif event_type == "callback_requested":
            if printed_prefix:
                print()
                printed_prefix = False
            await _handle_callback_requested(http, session_id, data)
        elif event_type == "error":
            print(f"\n[error] {data.get('message', 'unknown error')}")
        elif event_type == "turn_end":
            break
    if printed_prefix:
        print()


# ---------------------------------------------------------------------------
# The two screens
# ---------------------------------------------------------------------------


async def _answer_open_questions(http: httpx.AsyncClient, session_id: str) -> bool:
    """Show and answer the session's open ask_user questions. Returns True if any were found."""
    questions = _open_questions(await _status(http, session_id))
    if not questions:
        return False
    print(f"\nOpen questions ({len(questions)}):")
    for i, q in enumerate(questions, 1):
        text = q.get("tool_input", {}).get("question", "(no text)")
        print(f"  {i}. {text}   (turn {q.get('turn_number')})")

    answered: set[str] = set()
    for q in questions:
        text = q.get("tool_input", {}).get("question", "(the agent has a question)")
        print(f"\n  ? {text}")
        await _post_callback_result(
            http, session_id, q["tool_id"], result=await _ask("  your answer> ")
        )
        answered.add(q["tool_id"])
        print("  ✓ submitted")

    # The turn only proceeds once EVERY parked callback in the batch is answered, so watch it now.
    # We didn't start this turn, so observe it via /api/attach; from_offset=0 replays history, and
    # _observe suppresses it until our answers resolve (fine for an example — a resume_offset cursor
    # would trim the replay). Any follow-up ask_user in the same turn is handled inline by _observe.
    async with http.stream(
        "GET", "/api/attach", params={"session_id": session_id, "from_offset": 0}
    ) as resp:
        await _observe(http, session_id, resp, open_after=answered)
    return True


async def _chat_turn(http: httpx.AsyncClient, session_id: str, message: str) -> None:
    expected_turn = int((await _status(http, session_id)).get("current_turn", 0)) + 1
    body = {"session_id": session_id, "message": message, "expected_turn": expected_turn}
    async with http.stream("POST", "/api/chat", json=body) as resp:
        if resp.status_code != 200:
            print(f"[server error {resp.status_code}] {(await resp.aread()).decode()}")
            return
        await _observe(http, session_id, resp)


async def _session_view(http: httpx.AsyncClient, session_id: str) -> None:
    """Interact with one session: clear any waiting questions, then a chat REPL.

    Returns to the picker on `/sessions` or EOF; raises `_Quit` on `/quit`.
    """
    print(f"\nSession {session_id}")
    await _answer_open_questions(http, session_id)  # surface questions waiting from before we attached
    # Client-local navigation is keyed off ':' — deliberately NOT '/', which is reserved for the
    # harness's own slash commands (operator commands sent to the agent). ':' actions never touch
    # the agent; they just move around this CLI.
    print("\nChat with the agent. Commands: :questions  :sessions  :quit\n")
    while True:
        try:
            line = await _ask("you> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in {":quit", "quit", "exit"}:
            raise _Quit
        if line == ":sessions":
            return
        if line == ":questions":
            if not await _answer_open_questions(http, session_id):
                print("  (no open questions)")
            continue
        await _chat_turn(http, session_id, line)


async def _session_rows(
    http: httpx.AsyncClient,
) -> list[tuple[dict[str, Any], int]]:
    """Open ReactAgent sessions paired with their pending-ask_user count (one status call each)."""
    resp = await http.get("/api/sessions")
    resp.raise_for_status()
    sessions = [
        s
        for s in resp.json()
        if s.get("agent_workflow_type") == WORKFLOW_TYPE and not s.get("closed")
    ]

    async def _count(session: dict[str, Any]) -> int:
        try:
            return len(_open_questions(await _status(http, session["workflow_id"])))
        except httpx.HTTPError:
            return 0

    counts = await asyncio.gather(*(_count(s) for s in sessions))
    return list(zip(sessions, counts))


async def _pick_session(http: httpx.AsyncClient) -> str | None:
    """Render the home screen; return a session id to open, or None to quit."""
    while True:
        rows = await _session_rows(http)
        print("\nReAct agent client · sessions\n")
        if rows:
            print(f"  {'#':>2}  {'session':<30} {'pending':<9} opened with")
            for i, (s, count) in enumerate(rows, 1):
                sid = s["workflow_id"]
                short = (sid[:28] + "…") if len(sid) > 29 else sid
                pending = f"⏳ {count}" if count else "—"
                opened = (s.get("initial_user_message") or "").strip()
                if len(opened) > 40:
                    opened = opened[:39] + "…"
                tail = f" {opened!r}" if opened else ""
                print(f"  {i:>2}  {short:<30} {pending:<9}{tail}")
        else:
            print("  (no open sessions)")
        print("\n  n) new session   r) refresh   q) quit")
        try:
            choice = (await _ask("select> ")).lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if choice in {"q", "quit", "exit"}:
            return None
        if choice == "r":
            continue
        if choice == "n":
            sid = await _create_session(http)
            print(f"  created {sid}")
            return sid
        if choice.isdigit() and 1 <= int(choice) <= len(rows):
            return rows[int(choice) - 1][0]["workflow_id"]
        print("  ? enter a row number, or n / r / q")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server", default="http://localhost:8000", help="Harness server URL."
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Open this session id (workflow id) directly, skipping the picker.",
    )
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.server, timeout=None) as http:
        try:
            if args.session:
                await _session_view(http, args.session)
            while True:
                session_id = await _pick_session(http)
                if session_id is None:
                    break
                await _session_view(http, session_id)
        except _Quit:
            pass
    print("bye")


if __name__ == "__main__":
    asyncio.run(main())
