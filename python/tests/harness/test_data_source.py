# ABOUTME: Tests for data sources: data_source_tools turns a DataSource into search_<name> and
# fetch_<name> activity tools that an agent runs like any other tool, and the Jira and Notion
# sources send the requests their services expect, read their credential from the worker's
# environment, and stay inside their scope. The services are answered by a fake `send`.

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from pydantic import BaseModel
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.data_sources import JiraDataSource, NotionDataSource
from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.data_source import not_found
from temporal_agent_harness.plugin import AgentHarnessPlugin

# ---------------------------------------------------------------- the tools


class Handbook(agent.DataSource):
    name = "handbook"
    description = "The team handbook."

    PAGES = {"oncall": "Who to page, and when.", "release": "How to cut a release."}

    async def search(self, query: str, limit: int) -> list[agent.SearchResult]:
        return [
            agent.SearchResult(id=page, title=page.title(), snippet=text)
            for page, text in self.PAGES.items()
            if query in text
        ]

    async def fetch(self, id: str) -> agent.Document:
        if id not in self.PAGES:
            raise not_found(self.name, id)
        return agent.Document(id=id, title=id.title(), text=self.PAGES[id])


SEARCH, FETCH = agent.data_source_tools(Handbook())


class Found(BaseModel):
    hits: list[str]
    text: str


@agent.defn(name="HandbookReader")
class HandbookReader:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Search the handbook and read the first hit."""
        hits = await self._runner.run_tool("search-1", SEARCH, query=message.text)
        page = await self._runner.run_tool("fetch-1", FETCH, id=hits[0].id)
        found = Found(hits=[hit.id for hit in hits], text=page.text)
        return TextReply(text=found.model_dump_json())


def test_the_tools_are_named_and_described_for_the_source():
    assert (SEARCH.__name__, FETCH.__name__) == ("search_handbook", "fetch_handbook")
    assert "the team handbook" in SEARCH.__doc__.lower()
    assert "fetch_handbook" in SEARCH.__doc__
    names = {
        agent.tool_activity(tool).__temporal_activity_definition.name for tool in (SEARCH, FETCH)
    }
    assert names == {"search_handbook", "fetch_handbook"}


@pytest.mark.parametrize(("name", "description"), [("Hand Book", "x"), ("handbook", " ")])
def test_a_source_needs_a_plain_name_and_a_description(name: str, description: str):
    source = Handbook()
    source.name, source.description = name, description
    with pytest.raises(ValueError):
        agent.data_source_tools(source)


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"data-source-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[HandbookReader],
        plugins=[AgentHarnessPlugin(tools=[SEARCH, FETCH])],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def test_an_agent_runs_them_as_activity_tools_without_an_approval(client_and_queue):
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        "HandbookReader", AgentConfig(), id=f"reader-{uuid.uuid4()}", task_queue=task_queue
    )
    sent = await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="ask", payload={"text": "release"}),
        result_type=AgentMessageReply,
    )

    events: list[AgentEvent] = []
    stream = WorkflowStreamClient.create(client, handle.id).subscribe(
        topics=["turn_events"],
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    )
    async with asyncio.timeout(30):
        async for item in stream:
            events.append(item.data)
            if item.data.event.type == AgentEventType.TURN_END:
                break

    assert sent.turn_number == 1
    reply = next(e.event for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END)
    assert Found.model_validate_json(reply.output["text"]) == Found(
        hits=["release"], text="How to cut a release."
    )
    started = [e.event.tool_name for e in events if e.event.type == AgentEventType.TOOL_START]
    assert started == ["search_handbook", "fetch_handbook"]
    assert not [e for e in events if e.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED]


# ---------------------------------------------------------------- a fake service


class Service:
    """Answers each request from ``routes`` (path -> JSON, or a status), and keeps the requests."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []

    async def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.requests.append((method, url, dict(headers), json.loads(body) if body else None))
        parts = urlsplit(url)
        answer = self.routes.get(parts.path)
        if callable(answer):
            answer = answer(parse_qs(parts.query), json.loads(body) if body else None)
        if answer is None:
            return 404, b""
        if isinstance(answer, int):
            return answer, b""
        return 200, json.dumps(answer).encode()


# ---------------------------------------------------------------- Jira


def _jira(service: Service, **kwargs: Any) -> JiraDataSource:
    return JiraDataSource(base_url="https://acme.atlassian.net/", send=service.send, **kwargs)


@pytest.fixture
def jira_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_EMAIL", "bot@acme.example")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-token")


ISSUE = {
    "key": "AI-7",
    "fields": {
        "summary": "Idle close",
        "status": {"name": "Done"},
        "updated": "2026-01-02T03:04:05.000+0000",
    },
}


async def test_jira_searches_text_inside_its_scope(jira_env: None):
    service = Service({"/rest/api/3/search/jql": {"issues": [ISSUE]}})

    results = await _jira(service, jql_scope="project = AI").search('idle "close"', 5)

    assert results == [
        agent.SearchResult(
            id="AI-7",
            title="Idle close",
            url="https://acme.atlassian.net/browse/AI-7",
            snippet="Done",
            updated="2026-01-02T03:04:05.000+0000",
        )
    ]
    method, url, headers, _ = service.requests[0]
    query = parse_qs(urlsplit(url).query)
    assert method == "GET"
    assert query["jql"] == ['(project = AI) AND (text ~ "idle \\"close\\"") ORDER BY updated DESC']
    assert query["maxResults"] == ["5"]
    basic = base64.b64encode(b"bot@acme.example:jira-token").decode()
    assert headers["Authorization"] == f"Basic {basic}"


async def test_jira_searches_a_key_by_key(jira_env: None):
    service = Service({"/rest/api/3/search/jql": {"issues": []}})
    await _jira(service).search("AI-7", 5)
    assert parse_qs(urlsplit(service.requests[0][1]).query)["jql"] == [
        "key = AI-7 ORDER BY updated DESC"
    ]


async def test_jira_fetches_an_issue_with_its_comments(jira_env: None):
    fields = {
        **ISSUE["fields"],
        "description": "Close an agent that sits idle.",
        "assignee": {"displayName": "Ada"},
        "comment": {
            "comments": [
                {"author": {"displayName": "Bo"}, "created": "2026-01-03", "body": "Shipped."}
            ]
        },
    }
    service = Service({"/rest/api/2/issue/AI-7": {"key": "AI-7", "fields": fields}})

    issue = await _jira(service).fetch("ai-7")

    assert issue.title == "Idle close"
    assert issue.text == "Close an agent that sits idle.\n\nComment by Bo (2026-01-03):\nShipped."
    assert issue.metadata == {"status": "Done", "assignee": "Ada"}
    assert issue.url == "https://acme.atlassian.net/browse/AI-7"


async def test_jira_does_not_fetch_outside_its_scope(jira_env: None):
    service = Service(
        {
            "/rest/api/3/search/jql": {"issues": []},
            "/rest/api/2/issue/HR-1": {"key": "HR-1", "fields": {"summary": "Salaries"}},
        }
    )
    with pytest.raises(ApplicationError) as raised:
        await _jira(service, jql_scope="project = AI").fetch("HR-1")
    assert raised.value.type == "NotFound"
    assert [urlsplit(url).path for _, url, _, _ in service.requests] == ["/rest/api/3/search/jql"]
    assert parse_qs(urlsplit(service.requests[0][1]).query)["jql"] == [
        "(project = AI) AND (key = HR-1)"
    ]


@pytest.mark.parametrize("id", ["AI-7/../../myself", "not a key", "AI-999"])
async def test_jira_has_no_item_for_a_bad_or_unknown_id(jira_env: None, id: str):
    service = Service({})
    with pytest.raises(ApplicationError) as raised:
        await _jira(service).fetch(id)
    assert raised.value.type == "NotFound"
    assert all("/rest/api/2/issue/AI-999" in url for _, url, _, _ in service.requests)


async def test_a_source_without_its_credential_fails_without_a_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    service = Service({})
    with pytest.raises(ApplicationError) as raised:
        await _jira(service).search("x", 1)
    assert (raised.value.type, raised.value.non_retryable) == ("MissingCredential", True)
    assert service.requests == []


@pytest.mark.parametrize(
    ("status", "retried"), [(401, False), (403, False), (429, True), (503, True)]
)
async def test_a_refusal_is_final_and_an_overload_is_retried(
    jira_env: None, status: int, retried: bool
):
    service = Service({"/rest/api/3/search/jql": status})
    with pytest.raises(Exception) as raised:
        await _jira(service).search("x", 1)
    refused = isinstance(raised.value, ApplicationError) and raised.value.non_retryable
    assert refused is not retried
    assert "jira-token" not in str(raised.value)


# ---------------------------------------------------------------- Notion

PAGE_ID = "0123456789abcdef0123456789abcdef"
PAGE = {
    "id": "01234567-89ab-cdef-0123-456789abcdef",
    "url": "https://www.notion.so/Runbook-0123",
    "last_edited_time": "2026-02-03T04:05:00.000Z",
    "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "Run"}, {"plain_text": "book"}]}
    },
}


def _text(kind: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"id": f"b-{text}", "type": kind, kind: {"rich_text": [{"plain_text": text}], **extra}}


@pytest.fixture
def notion_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "notion-token")


async def test_notion_searches_pages(notion_env: None):
    service = Service({"/v1/search": {"results": [PAGE]}})

    results = await NotionDataSource(send=service.send).search("runbook", 3)

    assert results == [
        agent.SearchResult(
            id=PAGE["id"], title="Runbook", url=PAGE["url"], updated=PAGE["last_edited_time"]
        )
    ]
    method, url, headers, body = service.requests[0]
    assert (method, url) == ("POST", "https://api.notion.com/v1/search")
    assert body == {
        "query": "runbook",
        "page_size": 3,
        "filter": {"property": "object", "value": "page"},
    }
    assert headers["Authorization"] == "Bearer notion-token"
    assert "Notion-Version" in headers


async def test_notion_fetches_a_page_as_markdown(notion_env: None):
    def children(query: dict[str, list[str]], _: Any) -> dict[str, Any]:
        if "start_cursor" not in query:
            first = [
                _text("heading_1", "Steps"),
                {**_text("bulleted_list_item", "Deploy"), "has_children": True},
            ]
            return {"results": first, "has_more": True, "next_cursor": "c2"}
        last = [
            _text("to_do", "Tell the team", checked=True),
            _text("code", "make deploy"),
            {"id": "b-div", "type": "divider", "divider": {}},
            {
                "id": "b-sub",
                "type": "child_page",
                "child_page": {"title": "Rollback"},
                "has_children": True,
            },
        ]
        return {"results": last, "has_more": False}

    service = Service(
        {
            f"/v1/pages/{PAGE_ID}": PAGE,
            f"/v1/blocks/{PAGE_ID}/children": children,
            "/v1/blocks/b-Deploy/children": {"results": [_text("paragraph", "To staging first.")]},
        }
    )

    page = await NotionDataSource(send=service.send).fetch(PAGE["id"])

    assert page.title == "Runbook"
    assert page.text.splitlines() == [
        "# Steps",
        "- Deploy",
        "  To staging first.",
        "- [x] Tell the team",
        "```",
        "make deploy",
        "```",
        "[page: Rollback]",
    ]
    assert page.metadata == {}
    # The child page's own blocks are not read.
    assert not [url for _, url, _, _ in service.requests if "b-sub" in url]


async def test_notion_stops_at_max_blocks(notion_env: None):
    blocks = [_text("paragraph", f"line {n}") for n in range(5)]
    service = Service(
        {f"/v1/pages/{PAGE_ID}": PAGE, f"/v1/blocks/{PAGE_ID}/children": {"results": blocks}}
    )
    page = await NotionDataSource(send=service.send, max_blocks=2).fetch(PAGE_ID)
    assert page.text.splitlines() == ["line 0", "line 1"]
    assert page.metadata == {"truncated": "true"}


@pytest.mark.parametrize("id", ["../users/me", "0123", PAGE_ID])
async def test_notion_has_no_item_for_a_bad_or_unknown_id(notion_env: None, id: str):
    service = Service({})
    with pytest.raises(ApplicationError) as raised:
        await NotionDataSource(send=service.send).fetch(id)
    assert raised.value.type == "NotFound"
    assert all(url.endswith(f"/v1/pages/{PAGE_ID}") for _, url, _, _ in service.requests)
