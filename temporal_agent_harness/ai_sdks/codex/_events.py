"""Translate Codex CLI JSONL objects into the harness's native stream vocabulary.

Only documented, user-facing fields are exported. MCP arguments/results, patches,
and reasoning are deliberately omitted. Sanitization is best effort, not a promise
that arbitrary program output contains no sensitive information.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from urllib.parse import quote

from temporal_agent_harness.harness.agent_protocol import (
    AgentStreamItem,
    ReplyDelta,
    TokenUsage,
    ToolEndEvent,
    ToolErrorEvent,
    ToolProgressDelta,
    ToolRequested,
    ToolStartEvent,
)


MAX_TEXT_BYTES = 8 * 1024
MAX_TOOLS = 256
MAX_MESSAGES = 256
MAX_LIST_ITEMS = 32

# The key lookahead visits a word once; possessive matching prevents retrying every
# possible split of long identifiers such as "token" repeated thousands of times.
_SECRET_KEY = (
    r"(?<![\w-])[\"']?"
    r"(?=[\w-]*?(?:token|password|passwd|secret|api[_-]?key|authorization))"
    r"[\w-]++[\"']?"
)
_ASSIGNMENT = re.compile(
    rf"(?i)({_SECRET_KEY}\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s;&]+)"
)
_OPTION = re.compile(
    rf"(?i)({_SECRET_KEY}\s+)(?:\"[^\"]*\"|'[^']*'|[^\s;&]+)"
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)\S+")
_PROVIDER_KEY = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b"
)
_URL_AUTH = re.compile(r"(?i)(https?://)[^/\s@]++@")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
)


def _clip(value: str, limit: int = MAX_TEXT_BYTES) -> str:
    # Slice characters before encoding to bound allocation for enormous input.
    encoded = value[:limit + 1].encode("utf-8", errors="replace")
    if len(value) <= limit and len(encoded) <= limit:
        return encoded.decode("utf-8")
    return encoded[:limit - 3].decode("utf-8", errors="ignore") + "…"


def display_text(value: object, limit: int = MAX_TEXT_BYTES) -> str:
    """Return bounded display text with common credential forms removed.

    Pre-bounding keeps all redaction work and allocation independent of an
    untrusted source's size. Assignment matching also catches truncated values.
    """
    if not isinstance(value, str):
        return ""
    text = value[:MAX_TEXT_BYTES * 2]
    text = _ANSI.sub("", text)
    text = _PRIVATE_KEY.sub("[redacted private key]", text)
    text = _BEARER.sub(r"\1[redacted]", text)
    text = _ASSIGNMENT.sub(r"\1[redacted]", text)
    text = _OPTION.sub(r"\1[redacted]", text)
    text = _PROVIDER_KEY.sub("[redacted]", text)
    text = _URL_AUTH.sub(r"\1[redacted]@", text)
    # Preserve ordinary line breaks for command output while dropping controls.
    text = "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))
    return _clip(text, min(MAX_TEXT_BYTES, limit))


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


@dataclass
class _Tool:
    tool_id: str
    tool_name: str
    kind: str
    closed: bool = False
    progress: str = ""


class CodexEventMapper:
    """Synchronous, bounded adapter for one invocation of ``codex exec --json``.

    The caller brackets this mapper with model-interaction events and always
    calls :meth:`finish`, including on process errors and cancellation. Completed
    item IDs are retained to prevent duplicate lifecycle and reply events. Unknown
    events and malformed fields are ignored without preventing later valid events.
    """

    def __init__(
        self,
        publish: Callable[[AgentStreamItem], None],
        invocation_id: str,
    ) -> None:
        if not invocation_id or len(invocation_id) > 256:
            raise ValueError("invocation_id must contain 1 to 256 characters")
        self._publish = publish
        self._prefix = f"codex:{quote(invocation_id, safe='')}:"
        self._tools: dict[str, _Tool] = {}
        self._messages: set[str] = set()
        self._finished = False
        self._turn_completed = False
        self.usage: TokenUsage | None = None
        self.thread_id: str | None = None
        self.completed = False
        self.failure: str | None = None
        self.final_text = ""

    def on_event(self, event: dict) -> None:
        if self._finished or self.failure or not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and 0 < len(thread_id) <= 256:
                self.thread_id = thread_id
        elif event_type == "turn.started":
            self._turn_completed = False
            self.completed = False
        elif event_type == "turn.completed":
            if not self._turn_completed:
                self._add_usage(event.get("usage"))
                self._turn_completed = True
                self.completed = True
        elif event_type in ("turn.failed", "error"):
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else event.get("message")
            self.failure = display_text(message, 1024) or "Codex reported a failure."
            self.completed = False
            self._close_active(self.failure)
        elif event_type in ("item.started", "item.updated", "item.completed"):
            self._item(event_type, event.get("item"))

    def finish(self, error: str | None = None) -> None:
        """Resolve every started tool, even when the subprocess stops mid-item."""
        if self._finished:
            return
        if error:
            self.failure = self.failure or display_text(error, 1024) or "Codex failed."
            self.completed = False
        self._close_active(self.failure or "Codex ended before this tool completed.")
        self._finished = True

    def _close_active(self, message: str) -> None:
        for tool in self._tools.values():
            if not tool.closed:
                tool.closed = True
                self._publish(ToolErrorEvent(
                    tool_id=tool.tool_id, tool_name=tool.tool_name, message=message,
                ))

    def _add_usage(self, raw: object) -> None:
        if not isinstance(raw, dict):
            return
        counts = {
            "input_tokens": _count(raw.get("input_tokens")),
            "output_tokens": _count(raw.get("output_tokens")),
            "cached_tokens": _count(raw.get("cached_input_tokens")),
            "thought_tokens": _count(raw.get("reasoning_output_tokens")),
        }
        counts["total_tokens"] = _count(raw.get("total_tokens"))
        if counts["total_tokens"] is None:
            input_count, output_count = counts["input_tokens"], counts["output_tokens"]
            if input_count is not None and output_count is not None:
                counts["total_tokens"] = input_count + output_count
        if not any(count is not None for count in counts.values()):
            return
        for key, count in counts.items():
            previous = getattr(self.usage, key, None)
            if previous is not None:
                counts[key] = previous + (count or 0)
        self.usage = TokenUsage(**counts)

    def _item(self, event_type: str, item: object) -> None:
        if not isinstance(item, dict):
            return
        item_id, kind = item.get("id"), item.get("type")
        if not isinstance(item_id, str) or not 0 < len(item_id) <= 256:
            return
        if kind == "agent_message":
            # The public CLI format documents completed text snapshots, not token
            # deltas. Waiting for completion also avoids redaction across fragments.
            if event_type == "item.completed" and item_id not in self._messages:
                text = display_text(item.get("text"))
                if text and len(self._messages) < MAX_MESSAGES:
                    self._messages.add(item_id)
                    self.final_text = text
                    self._publish(ReplyDelta(text=text))
            return
        names = {
            "command_execution": "codex.command",
            "file_change": "codex.file_change",
            "mcp_tool_call": "codex.mcp_tool_call",
            "web_search": "codex.web_search",
            "todo_list": "codex.plan",
        }
        if not isinstance(kind, str) or kind not in names:
            return
        tool = self._tools.get(item_id)
        if tool is None:
            if len(self._tools) >= MAX_TOOLS:
                return
            tool = _Tool(self._prefix + quote(item_id, safe=""), names[kind], kind)
            self._tools[item_id] = tool
            inputs = self._inputs(item, kind)
            self._publish(ToolRequested(
                tool_id=tool.tool_id, tool_name=tool.tool_name, tool_input=inputs,
            ))
            self._publish(ToolStartEvent(
                tool_id=tool.tool_id, tool_name=tool.tool_name, tool_input=inputs,
            ))
        if tool.closed or tool.kind != kind:
            return
        progress = self._summary(item, kind)
        if progress and progress != tool.progress:
            # Command updates contain accumulated output, so publish only the new
            # suffix when possible. Plans/file lists are compact status snapshots.
            delta = progress
            if kind == "command_execution" and progress.startswith(tool.progress):
                delta = progress[len(tool.progress):]
            tool.progress = progress
            if delta and event_type != "item.completed":
                self._publish(ToolProgressDelta(
                    tool_id=tool.tool_id, tool_name=tool.tool_name, progress_delta=delta,
                ))
        if event_type == "item.completed":
            tool.closed = True
            failed = item.get("status") in ("failed", "declined", "cancelled")
            code = item.get("exit_code")
            # JSON numbers are untrusted too; avoid formatting gigantic integers.
            if type(code) is not int or not -(2**31) <= code < 2**31:
                code = None
            if kind == "command_execution" and code is not None and code != 0:
                failed = True
            if failed:
                message = f"Command exited with code {code}." if kind == "command_execution" and code is not None else "Codex tool failed."
                self._publish(ToolErrorEvent(
                    tool_id=tool.tool_id, tool_name=tool.tool_name,
                    message=display_text(f"{message}\n{progress}".rstrip()),
                ))
            else:
                if kind == "command_execution" and code is not None:
                    progress = display_text(f"Exit code: {code}\n{progress}".rstrip())
                self._publish(ToolEndEvent(
                    tool_id=tool.tool_id, tool_name=tool.tool_name,
                    tool_output=progress or "Completed.",
                ))

    @staticmethod
    def _inputs(item: dict, kind: str) -> dict:
        if kind == "command_execution":
            return {"command": display_text(item.get("command"))}
        if kind == "mcp_tool_call":
            return {
                "server": display_text(item.get("server"), 256),
                "tool": display_text(item.get("tool"), 256),
            }
        if kind == "web_search":
            return {"query": display_text(item.get("query"), 1024)}
        return {"summary": CodexEventMapper._summary(item, kind)}

    @staticmethod
    def _summary(item: dict, kind: str) -> str:
        if kind == "command_execution":
            return display_text(item.get("aggregated_output"))
        if kind == "mcp_tool_call":
            return display_text(f"{display_text(item.get('server'), 256)} / {display_text(item.get('tool'), 256)}")
        if kind == "web_search":
            return display_text(item.get("query"), 1024)
        if kind == "file_change":
            changes = item.get("changes")
            if not isinstance(changes, list):
                return ""
            rows = []
            for change in changes[:MAX_LIST_ITEMS]:
                if isinstance(change, dict) and isinstance(change.get("path"), str):
                    rows.append(f"{display_text(change.get('kind'), 24) or 'change'}: {display_text(change['path'], 180)}")
            if len(changes) > MAX_LIST_ITEMS:
                rows.append(f"… {len(changes) - MAX_LIST_ITEMS} more changes")
            return display_text("\n".join(rows))
        if kind == "todo_list":
            items = item.get("items")
            if not isinstance(items, list):
                return ""
            rows = []
            for entry in items[:MAX_LIST_ITEMS]:
                if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                    marker = "x" if entry.get("completed") is True else " "
                    rows.append(f"[{marker}] {display_text(entry['text'], 180)}")
            if len(items) > MAX_LIST_ITEMS:
                rows.append(f"… {len(items) - MAX_LIST_ITEMS} more steps")
            return display_text("\n".join(rows))
        return ""
