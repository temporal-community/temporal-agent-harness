"""A thin terminal client for the wiki callback-tools agent.

This is the piece that makes callback tools real: the agent runs on a Temporal worker (imagine
it in the cloud) with no access to your disk, so when it wants to read or write your wiki it
publishes a ``callback_requested`` event and waits. THIS client — running on your machine —
watches the agent's event stream, executes each requested tool against a LOCAL wiki directory,
and posts the result back so the agent can continue.

It talks only to the packaged harness HTTP server (``app.py``); it needs no Temporal client of its
own. Per user message it:

  1. reads the agent's current turn (``GET /api/status``) to compute ``expected_turn``;
  2. sends the message and streams that turn (``POST /api/chat``, Server-Sent Events);
  3. on each ``callback_requested`` event, runs the tool locally and posts the result
     (``POST /api/callback-result``) — the new route this example adds;
  4. prints the assistant's streamed reply.

There is deliberately no "advertising" of which tools this client implements: if the agent calls
a tool this client doesn't know, the call simply sits unresolved (visible under
``agent_status.pending_callbacks``) until something fulfills it.

Run from the repo root (with the server + workers already up — see README.md):

    uv run --group examples python -m examples.callback_tools.wiki_agent.client --wiki-dir ./wiki
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx

WORKFLOW_TYPE = "WikiAgent"


# ---------------------------------------------------------------------------
# Local wiki tool implementations — the actual work, on THIS machine.
# ---------------------------------------------------------------------------


def _resolve(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under the wiki ``root``, refusing paths that escape it."""
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"path {rel!r} escapes the wiki root")
    return target


def _ls(root: Path, path: str) -> list[str]:
    directory = _resolve(root, path)
    if not directory.is_dir():
        raise NotADirectoryError(f"{path!r} is not a directory")
    entries = []
    for child in sorted(directory.iterdir(), key=lambda p: p.name):
        entries.append(child.name + "/" if child.is_dir() else child.name)
    return entries


def _tree(root: Path, path: str) -> str:
    base = _resolve(root, path)
    if not base.exists():
        raise FileNotFoundError(f"{path!r} does not exist")
    label = path if path not in (".", "") else "."
    lines = [label if label.endswith("/") or base.is_file() else f"{label}/"]

    def walk(directory: Path, prefix: str) -> None:
        children = sorted(
            directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
        )
        for child in children:
            marker = "/" if child.is_dir() else ""
            lines.append(f"{prefix}{child.name}{marker}")
            if child.is_dir():
                walk(child, prefix + "  ")

    if base.is_dir():
        walk(base, "  ")
    return "\n".join(lines)


def _read_file(root: Path, path: str) -> str:
    return _resolve(root, path).read_text(encoding="utf-8")


def _write_file(root: Path, path: str, content: str) -> str:
    target = _resolve(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} characters to {path}"


def _delete_file(root: Path, path: str) -> str:
    target = _resolve(root, path)
    if not target.is_file():
        raise FileNotFoundError(f"{path!r} is not a file")
    target.unlink()
    return f"deleted {path}"


def _grep(root: Path, pattern: str, *, max_results: int = 200) -> list[str]:
    regex = re.compile(pattern)
    root_resolved = root.resolve()
    matches: list[str] = []
    for md in sorted(root_resolved.rglob("*.md")):
        rel = md.relative_to(root_resolved).as_posix()
        try:
            text = md.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                if len(matches) >= max_results:
                    return matches
    return matches


def execute_callback(
    root: Path, tool_name: str, tool_input: dict[str, Any]
) -> tuple[Any, str | None]:
    """Run one callback tool locally. Returns ``(result, None)`` on success, or
    ``(None, error_message)`` if the tool is unknown or the operation fails — the error is sent
    back to the agent as the tool's error result (the turn continues; the model sees it)."""
    try:
        if tool_name == "ls":
            return _ls(root, tool_input["path"]), None
        if tool_name == "tree":
            return _tree(root, tool_input["path"]), None
        if tool_name == "read_file":
            return _read_file(root, tool_input["path"]), None
        if tool_name == "write_file":
            return _write_file(root, tool_input["path"], tool_input["content"]), None
        if tool_name == "delete_file":
            return _delete_file(root, tool_input["path"]), None
        if tool_name == "grep":
            return _grep(root, tool_input["pattern"]), None
        return None, f"this client does not implement the tool {tool_name!r}"
    except Exception as e:  # noqa: BLE001 — any local failure becomes a tool error result
        return None, f"{type(e).__name__}: {e}"


def _summarize_args(tool_input: dict[str, Any]) -> str:
    """Compact one-line rendering of a call's args for the terminal (never dumps file bodies)."""
    parts = []
    for key, value in tool_input.items():
        if key == "content" and isinstance(value, str):
            parts.append(f"content=<{len(value)} chars>")
        else:
            parts.append(f"{key}={value!r}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# HTTP glue
# ---------------------------------------------------------------------------


async def _create_session(http: httpx.AsyncClient) -> str:
    resp = await http.post(
        "/api/sessions", json={"agent_workflow_type": WORKFLOW_TYPE}
    )
    resp.raise_for_status()
    return resp.json()["workflow_id"]


async def _current_turn(http: httpx.AsyncClient, session_id: str) -> int:
    resp = await http.get(f"/api/status/{session_id}")
    resp.raise_for_status()
    return int(resp.json().get("current_turn", 0))


async def _post_callback_result(
    http: httpx.AsyncClient,
    session_id: str,
    tool_id: str,
    result: Any,
    error: str | None,
) -> None:
    body: dict[str, Any] = {"session_id": session_id, "tool_id": tool_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    resp = await http.post("/api/callback-result", json=body)
    if resp.status_code != 200:
        print(f"\n  ! failed to post callback result: {resp.status_code} {resp.text}")


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


async def _run_turn(
    http: httpx.AsyncClient, session_id: str, wiki_root: Path, message: str
) -> None:
    expected_turn = await _current_turn(http, session_id) + 1
    body = {
        "session_id": session_id,
        "message": message,
        "expected_turn": expected_turn,
    }
    printed_reply_prefix = False
    async with http.stream("POST", "/api/chat", json=body) as resp:
        if resp.status_code != 200:
            text = (await resp.aread()).decode()
            print(f"[server error {resp.status_code}] {text}")
            return
        async for event_type, data in _iter_sse(resp):
            if event_type == "callback_requested":
                tool_name = data.get("tool_name", "?")
                tool_id = data["tool_id"]
                tool_input = data.get("tool_input", {})
                result, error = execute_callback(wiki_root, tool_name, tool_input)
                summary = error if error is not None else _result_summary(result)
                print(f"  · {tool_name}({_summarize_args(tool_input)}) → {summary}")
                await _post_callback_result(
                    http, session_id, tool_id, result, error
                )
            elif event_type == "reply_delta":
                if not printed_reply_prefix:
                    print("\nwiki> ", end="", flush=True)
                    printed_reply_prefix = True
                print(data.get("text", ""), end="", flush=True)
            elif event_type == "error":
                print(f"\n[error] {data.get('message', 'unknown error')}")
    if printed_reply_prefix:
        print()  # end the streamed reply line


def _result_summary(result: Any) -> str:
    if isinstance(result, list):
        return f"{len(result)} line(s)"
    if isinstance(result, str):
        first = result.splitlines()[0] if result else ""
        return f"{len(result)} chars" if len(result) > 40 else (first or "ok")
    return "ok"


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://localhost:8000", help="Harness server URL.")
    parser.add_argument(
        "--wiki-dir",
        default="./wiki",
        help="Local directory the wiki lives in (created if missing).",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Existing session id (workflow id) to attach to; a new one is created if omitted.",
    )
    args = parser.parse_args()

    wiki_root = Path(args.wiki_dir).expanduser()
    wiki_root.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(base_url=args.server, timeout=None) as http:
        session_id = args.session or await _create_session(http)
        print(f"Wiki keeper session: {session_id}")
        print(f"Wiki directory:      {wiki_root.resolve()}")
        print("Chat with the wiki keeper. Type 'exit' or 'quit' (or Ctrl-D) to leave.\n")

        while True:
            try:
                message = (await asyncio.to_thread(input, "you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not message:
                continue
            if message.lower() in {"exit", "quit"}:
                break
            try:
                await _run_turn(http, session_id, wiki_root, message)
            except httpx.HTTPError as e:
                print(f"[connection error] {e}")


if __name__ == "__main__":
    asyncio.run(main())
