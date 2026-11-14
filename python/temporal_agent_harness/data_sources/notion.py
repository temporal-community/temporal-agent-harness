"""Notion pages as a data source."""

from __future__ import annotations

import os
import re
from typing import Any

from temporal_agent_harness.data_sources import _http
from temporal_agent_harness.harness.data_source import (
    DataSource,
    Document,
    SearchResult,
    missing_credential,
    not_found,
)

_API = "https://api.notion.com/v1"
_VERSION = "2022-06-28"
_ID = re.compile(r"[0-9a-f]{32}")

_PREFIX = {
    "heading_1": "# ",
    "heading_2": "## ",
    "heading_3": "### ",
    "bulleted_list_item": "- ",
    "numbered_list_item": "1. ",
    "quote": "> ",
}


def _plain(rich_text: list[dict[str, Any]] | None) -> str:
    return "".join(part.get("plain_text", "") for part in rich_text or [])


def _title(page: dict[str, Any]) -> str:
    for value in (page.get("properties") or {}).values():
        if value.get("type") == "title":
            return _plain(value.get("title"))
    return ""


def _line(block: dict[str, Any]) -> str | None:
    """A block as one line of Markdown, or ``None`` for a block that has no text."""
    kind = block.get("type", "")
    content = block.get(kind)
    if not isinstance(content, dict):
        return None
    if kind == "child_page":
        return f"[page: {content.get('title', '')}]"
    text = _plain(content.get("rich_text"))
    if kind == "to_do":
        return f"- [{'x' if content.get('checked') else ' '}] {text}"
    if kind == "code":
        return f"```\n{text}\n```"
    if not text:
        return None
    return f"{_PREFIX.get(kind, '')}{text}"


class NotionDataSource(DataSource):
    """Pages of a Notion workspace: ``search`` finds pages by title, and ``fetch`` reads a
    page's blocks as Markdown.

    It sees the pages that are shared with the integration whose token is in ``token_env`` on
    the worker. That sharing is the scope. ``max_blocks`` bounds how much of a long page one
    ``fetch`` reads.
    """

    def __init__(
        self,
        *,
        name: str = "notion",
        description: str = "Notion pages of the workspace",
        token_env: str = "NOTION_TOKEN",
        max_blocks: int = 1000,
        send: _http.Send = _http.send,
    ) -> None:
        self.name = name
        self.description = description
        self._token_env = token_env
        self._max_blocks = max_blocks
        self._send = send

    def _headers(self) -> dict[str, str]:
        token = os.environ.get(self._token_env)
        if not token:
            raise missing_credential(self.name, self._token_env)
        return {"Authorization": f"Bearer {token}", "Notion-Version": _VERSION}

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        return await _http.request_json(
            self.name, self._send, method, f"{_API}{path}", self._headers(), body
        )

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        found = await self._call(
            "POST",
            "/search",
            {
                "query": query,
                "page_size": limit,
                "filter": {"property": "object", "value": "page"},
            },
        )
        return [
            SearchResult(
                id=page["id"],
                title=_title(page) or "Untitled",
                url=page.get("url"),
                updated=page.get("last_edited_time"),
            )
            for page in (found or {}).get("results", [])
        ]

    async def _blocks(self, block_id: str, depth: int, lines: list[str], left: list[int]) -> None:
        cursor: str | None = None
        while left[0] > 0:
            query = "?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            page = await self._call("GET", f"/blocks/{block_id}/children{query}")
            if page is None:
                return
            for block in page.get("results", []):
                if left[0] <= 0:
                    return
                left[0] -= 1
                line = _line(block)
                if line is not None:
                    lines.append("  " * depth + line)
                # A child page is a page of its own: fetch it by its id.
                if block.get("has_children") and block.get("type") != "child_page":
                    await self._blocks(block["id"], depth + 1, lines, left)
            cursor = page.get("next_cursor") if page.get("has_more") else None
            if cursor is None:
                return

    async def fetch(self, id: str) -> Document:
        page_id = id.strip().replace("-", "").lower()
        if not _ID.fullmatch(page_id):
            raise not_found(self.name, id)
        page = await self._call("GET", f"/pages/{page_id}")
        if page is None:
            raise not_found(self.name, id)
        lines: list[str] = []
        left = [self._max_blocks]
        await self._blocks(page_id, 0, lines, left)
        metadata = {"truncated": "true"} if left[0] <= 0 else {}
        return Document(
            id=page["id"],
            title=_title(page) or "Untitled",
            text="\n".join(lines),
            url=page.get("url"),
            updated=page.get("last_edited_time"),
            metadata=metadata,
        )
