"""Jira Cloud issues as a data source."""

from __future__ import annotations

import base64
import os
import re
from urllib.parse import quote, urlencode

from temporal_agent_harness.data_sources import _http
from temporal_agent_harness.harness.data_source import (
    DataSource,
    Document,
    SearchResult,
    missing_credential,
    not_found,
)

_KEY = re.compile(r"[A-Z][A-Z0-9_]*-[0-9]+")


class JiraDataSource(DataSource):
    """Issues of a Jira Cloud site: ``search`` is a text search, and ``fetch`` reads one issue
    with its comments by key.

    ``jql_scope`` limits both to the issues that a JQL clause matches, such as
    ``"project = AI"``. The worker's environment holds the account's email and API token, in
    ``email_env`` and ``token_env``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        name: str = "jira",
        description: str | None = None,
        jql_scope: str | None = None,
        email_env: str = "JIRA_EMAIL",
        token_env: str = "JIRA_API_TOKEN",
        send: _http.Send = _http.send,
    ) -> None:
        self.name = name
        self.description = description or f"Jira issues at {base_url}"
        self._base_url = base_url.rstrip("/")
        self._scope = jql_scope
        self._email_env = email_env
        self._token_env = token_env
        self._send = send

    def _headers(self) -> dict[str, str]:
        email = os.environ.get(self._email_env)
        token = os.environ.get(self._token_env)
        if not email:
            raise missing_credential(self.name, self._email_env)
        if not token:
            raise missing_credential(self.name, self._token_env)
        basic = base64.b64encode(f"{email}:{token}".encode()).decode()
        return {"Authorization": f"Basic {basic}"}

    def _scoped(self, jql: str) -> str:
        return jql if self._scope is None else f"({self._scope}) AND ({jql})"

    async def _search(self, jql: str, limit: int) -> list[dict]:
        query = urlencode({"jql": jql, "maxResults": limit, "fields": "summary,status,updated"})
        found = await _http.request_json(
            self.name,
            self._send,
            "GET",
            f"{self._base_url}/rest/api/3/search/jql?{query}",
            self._headers(),
        )
        return (found or {}).get("issues", [])

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        text = query.strip()
        if _KEY.fullmatch(text):
            jql = f"key = {text}"
        else:
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            jql = f'text ~ "{escaped}"'
        issues = await self._search(f"{self._scoped(jql)} ORDER BY updated DESC", limit)
        return [
            SearchResult(
                id=issue["key"],
                title=issue["fields"].get("summary") or issue["key"],
                url=f"{self._base_url}/browse/{issue['key']}",
                snippet=(issue["fields"].get("status") or {}).get("name", ""),
                updated=issue["fields"].get("updated"),
            )
            for issue in issues
        ]

    async def fetch(self, id: str) -> Document:
        key = id.strip().upper()
        if not _KEY.fullmatch(key):
            raise not_found(self.name, id)
        # A key outside the scope is answered as if the issue did not exist.
        if self._scope is not None and not await self._search(self._scoped(f"key = {key}"), 1):
            raise not_found(self.name, id)
        fields = "summary,description,status,assignee,updated,comment"
        # API version 2 gives the description and the comments as text, not as a document tree.
        issue = await _http.request_json(
            self.name,
            self._send,
            "GET",
            f"{self._base_url}/rest/api/2/issue/{quote(key)}?fields={fields}",
            self._headers(),
        )
        if issue is None:
            raise not_found(self.name, id)
        got = issue["fields"]
        parts = [got.get("description") or ""]
        for comment in (got.get("comment") or {}).get("comments", []):
            author = (comment.get("author") or {}).get("displayName", "unknown")
            parts.append(f"Comment by {author} ({comment.get('created', '')}):\n{comment['body']}")
        metadata = {"status": (got.get("status") or {}).get("name", "")}
        if got.get("assignee"):
            metadata["assignee"] = got["assignee"].get("displayName", "")
        return Document(
            id=key,
            title=got.get("summary") or key,
            text="\n\n".join(part for part in parts if part),
            url=f"{self._base_url}/browse/{key}",
            updated=got.get("updated"),
            metadata=metadata,
        )
