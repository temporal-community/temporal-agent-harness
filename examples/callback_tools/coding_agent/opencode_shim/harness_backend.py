"""A Temporal-harness-backed :class:`AgentBackend` for the coding agent.

This is the shim's real front end for the durable ``CodingAgent`` workflow. It reaches the agent
**through the packaged harness HTTP server** (``examples/app.py`` on :8000) — the same server the
Svelte UI uses — so every session is created via the session-manager and is visible/debuggable in
that UI while the user chats in OpenCode.

Per user prompt, :meth:`run_turn`:

  1. ensures a harness session exists for this OpenCode session (``POST /api/sessions`` →
     ``SessionManagerWorkflow`` launches the ``CodingAgent`` child workflow), mapping the OpenCode
     session id to the harness ``workflow_id``;
  2. sends the prompt and streams the turn (``POST /api/chat``, Server-Sent Events);
  3. translates each harness ``AgentEvent`` into calls on the :class:`AgentTurn` the shim gave us:

        reply_delta            -> turn.stream_text
        tool_approval_requested-> turn.request_permission  -> POST /api/approve
        tool_start             -> turn.tool_start (a running tool card)
        callback_requested     -> run the tool locally, POST /api/callback-result, complete the card
        tool_error             -> handle.error

Approvals and callbacks are handled in background tasks so the SSE loop keeps draining while a
permission prompt or a slow ``bash`` is outstanding (and so parallel tool calls don't serialize).

``POST /session/{id}/abort`` cancels the shim's ``run_turn`` task; we cancel any in-flight
approval/callback tasks. (The harness turn keeps running server-side — true turn cancellation
would need a cancel signal on the workflow; noted as a follow-up.)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx

from . import local_tools
from .backend import AgentTurn, ToolHandle


def _camel(key: str) -> str:
    """snake_case -> camelCase, so tool args render with OpenCode's canonical keys
    (``file_path`` -> ``filePath``, ``old_string`` -> ``oldString``)."""
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def _opencode_args(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Render args under OpenCode's camelCase keys so the TUI's rich tool cards light up."""
    return {_camel(k): v for k, v in tool_input.items()}


def _approval_patterns(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    """A short, human-readable preview of the gated call for the OpenCode permission prompt."""
    if tool_name == "bash" and "command" in tool_input:
        return [str(tool_input["command"])]
    for key in ("file_path", "pattern"):
        if key in tool_input:
            return [f"{tool_name} {tool_input[key]}"]
    return [tool_name]


async def _iter_sse(resp: httpx.Response) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
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


class HarnessBackend:
    """Fronts a Temporal ``CodingAgent`` workflow via the packaged harness HTTP server."""

    def __init__(
        self,
        *,
        server_url: str,
        working_dir: str,
        workflow_type: str = "CodingAgent",
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._root = Path(working_dir).resolve()
        self._workflow_type = workflow_type
        # OpenCode session id -> harness workflow id (the durable session).
        self._sessions: dict[str, str] = {}
        self._http = httpx.AsyncClient(base_url=self._server_url, timeout=httpx.Timeout(None))

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ the seam

    async def run_turn(self, turn: AgentTurn) -> None:
        harness_id = await self._ensure_session(turn.session_id)
        expected_turn = await self._current_turn(harness_id) + 1
        body = {
            "session_id": harness_id,
            "message": turn.prompt_text,
            "expected_turn": expected_turn,
        }

        handles: dict[str, ToolHandle] = {}
        tasks: list[asyncio.Task[Any]] = []
        try:
            async with self._http.stream("POST", "/api/chat", json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode()
                    raise RuntimeError(f"/api/chat failed: {resp.status_code} {detail}")
                async for event_type, data in _iter_sse(resp):
                    await self._handle_event(turn, harness_id, event_type, data, handles, tasks)
        except asyncio.CancelledError:
            # The user aborted (POST /session/{id}/abort cancels this task). Really stop the
            # durable agent — the harness `close` signal winds down its turn loop and auto-denies
            # pending gates — then forget the mapping so the next prompt starts a fresh session.
            for task in tasks:
                task.cancel()
            self._sessions.pop(turn.session_id, None)
            await asyncio.shield(self._close_harness(harness_id))
            raise
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_event(
        self,
        turn: AgentTurn,
        harness_id: str,
        event_type: str,
        data: dict[str, Any],
        handles: dict[str, ToolHandle],
        tasks: list[asyncio.Task[Any]],
    ) -> None:
        if event_type == "reply_delta":
            text = data.get("text", "")
            if text:
                await turn.stream_text(text)

        elif event_type == "thought_summary":
            # The model's streamed thinking. ThoughtSummaryDelta carries the raw Gemini
            # DeltaThoughtSummary under `delta`; its `content` is a TextContent object
            # (``{"type": "text", "text": ...}``), not a bare string.
            content = (data.get("delta") or {}).get("content")
            text = content.get("text", "") if isinstance(content, dict) else (content or "")
            if text:
                await turn.stream_reasoning(text)

        elif event_type == "tool_approval_requested":
            tool_id = data["tool_id"]
            tool_name = data["tool_name"]
            tool_input = data.get("tool_input", {})
            # Show the tool card BEFORE the prompt. OpenCode's permission dialog reads the
            # command/args to display from the matching tool PART's `input` (correlated by
            # callID/messageID), not from the permission payload — so the card must already exist,
            # in a non-pending state, when the prompt renders. Without this the prompt body is
            # empty (e.g. a bash permission with no command shown).
            if tool_id not in handles:
                handles[tool_id] = await turn.tool_start(
                    tool_name, _opencode_args(tool_input), call_id=tool_id
                )
            tasks.append(
                asyncio.create_task(
                    self._handle_approval(
                        turn,
                        harness_id,
                        handles,
                        tool_id=tool_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                    )
                )
            )

        elif event_type == "tool_start":
            # Usually the card was already created at approval time (above); only create it here
            # if it wasn't (e.g. an auto-approved tool that skipped the prompt).
            tool_id = data["tool_id"]
            if tool_id not in handles:
                handles[tool_id] = await turn.tool_start(
                    data["tool_name"],
                    _opencode_args(data.get("tool_input", {})),
                    call_id=tool_id,
                )

        elif event_type == "callback_requested":
            # Hand the card to the callback handler (pop it out of `handles`) so the fallback
            # tool_end/tool_error paths below won't also complete it — the handler is the sole
            # completer for a callback tool (it has the metadata, e.g. bash's exit code).
            tasks.append(
                asyncio.create_task(
                    self._handle_callback(
                        turn,
                        harness_id,
                        tool_id=data["tool_id"],
                        tool_name=data["tool_name"],
                        tool_input=data.get("tool_input", {}),
                        handle=handles.pop(data["tool_id"], None),
                    )
                )
            )

        elif event_type == "tool_end":
            # Completes any card NOT owned by a callback handler — i.e. an inline (non-callback)
            # tool like `todowrite`, or a fallback. `todowrite` runs in the workflow, so there's no
            # callback_requested; render its checklist (metadata.todos, from the card's input) and
            # back the live todo panel here.
            handle = handles.pop(data["tool_id"], None)
            if handle is not None:
                if data.get("tool_name") == "todowrite":
                    todos = handle._input().get("todos", [])
                    turn.set_todos(todos)
                    await handle.complete(data.get("tool_output", ""), metadata={"todos": todos})
                else:
                    await handle.complete(data.get("tool_output", ""))

        elif event_type == "tool_error":
            handle = handles.pop(data["tool_id"], None)
            if handle is not None:
                await handle.error(data.get("message", "tool error"))

        elif event_type == "error":
            raise RuntimeError(data.get("message", "agent error"))

        # Ignored: tool_requested, tool_approval_resolved, callback_resolved, turn_started,
        # turn_end, reply — the shim's own bookkeeping (begin/finish) and reply_delta cover them.

    @staticmethod
    def _permission_type(tool_name: str) -> str:
        """The permission type OpenCode's dialog renders. OpenCode has no ``write`` case (it would
        fall back to a bare "Call tool write"), so present a write as an ``edit`` — its prompt shows
        the file path and a diff of what will be written."""
        return "edit" if tool_name == "write" else tool_name

    def _permission_metadata(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Extra render data the permission dialog reads directly (not from the tool part).

        Only the edit-style prompts (``edit`` and ``write``, which we render as an edit) use it:
        OpenCode shows ``metadata.filepath`` as the title and ``metadata.diff`` in its diff viewer.
        We have the file on disk, so we compute the diff of what the call will do (best effort).
        Every other tool reads its args from the tool part's input, so an empty dict is fine.
        """
        if tool_name not in ("edit", "write"):
            return {}
        file_path = str(tool_input.get("file_path", ""))
        try:
            before = (self._root / file_path).read_text(encoding="utf-8")
        except OSError:
            before = ""  # new file / unreadable — diff against empty
        if tool_name == "write":
            after = str(tool_input.get("content", ""))
        else:
            old_string = tool_input.get("old_string", "")
            after = (
                before.replace(old_string, str(tool_input.get("new_string", "")), 1)
                if old_string and old_string in before
                else before
            )
        meta: dict[str, Any] = {"filepath": file_path}
        diff = local_tools.unified_diff(before, after, file_path)
        if diff:
            meta["diff"] = diff
        return meta

    async def _handle_approval(
        self,
        turn: AgentTurn,
        harness_id: str,
        handles: dict[str, ToolHandle],
        *,
        tool_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> None:
        """Turn a gated call into an OpenCode permission prompt, then relay the decision."""
        reply = await turn.request_permission(
            self._permission_type(tool_name),
            patterns=_approval_patterns(tool_name, tool_input),
            metadata=self._permission_metadata(tool_name, tool_input),
            call_id=tool_id,
        )
        approved = reply in ("once", "always")
        # On rejection the tool never executes (no callback_requested arrives to resolve the card),
        # so close out the card we showed for the prompt. Popping keeps the tool_end/tool_error
        # fallback from double-handling it.
        if not approved:
            handle = handles.pop(tool_id, None)
            if handle is not None:
                await handle.error("Rejected by user")
        await self._post_approve(
            harness_id,
            tool_id,
            approved=approved,
            remember=reply == "always",
        )

    async def _handle_callback(
        self,
        turn: AgentTurn,
        harness_id: str,
        *,
        tool_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        handle: ToolHandle | None,
    ) -> None:
        """Execute a callback tool locally, post the result to the workflow, and complete the card."""
        output, metadata, error = await local_tools.execute(self._root, tool_name, tool_input)
        await self._post_callback_result(harness_id, tool_id, result=output, error=error)
        if handle is not None:
            if error is not None:
                await handle.error(error)
            else:
                await handle.complete(output or "", metadata=metadata or {})

    # ------------------------------------------------------------------ HTTP glue

    async def _close_harness(self, harness_id: str) -> None:
        """Best-effort: signal the workflow to close. Swallow errors — the turn is already ending."""
        try:
            await self._http.post(f"/api/sessions/{harness_id}/close")
        except httpx.HTTPError:
            pass

    async def _ensure_session(self, opencode_session_id: str) -> str:
        existing = self._sessions.get(opencode_session_id)
        if existing is not None:
            return existing
        resp = await self._http.post(
            "/api/sessions", json={"agent_workflow_type": self._workflow_type}
        )
        resp.raise_for_status()
        workflow_id = resp.json()["workflow_id"]
        self._sessions[opencode_session_id] = workflow_id
        return workflow_id

    async def _current_turn(self, harness_id: str) -> int:
        resp = await self._http.get(f"/api/status/{harness_id}")
        resp.raise_for_status()
        return int(resp.json().get("current_turn", 0))

    async def _post_approve(
        self, harness_id: str, tool_id: str, *, approved: bool, remember: bool
    ) -> None:
        resp = await self._http.post(
            "/api/approve",
            json={
                "session_id": harness_id,
                "tool_id": tool_id,
                "approved": approved,
                "remember": remember,
            },
        )
        resp.raise_for_status()

    async def _post_callback_result(
        self, harness_id: str, tool_id: str, *, result: Any, error: str | None
    ) -> None:
        body: dict[str, Any] = {"session_id": harness_id, "tool_id": tool_id}
        if error is not None:
            body["error"] = error
        else:
            body["result"] = result
        resp = await self._http.post("/api/callback-result", json=body)
        resp.raise_for_status()
