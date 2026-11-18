"""``DataSource``: read access to a system of record, as two tools an agent can call.

A data source answers two questions: *what is there about X* (``search``) and *give me that
one* (``fetch``). ``data_source_tools(source)`` turns it into two activity tools,
``search_<name>`` and ``fetch_<name>``, so a model uses every source the same way, and Code
Mode scripts can search one source and fetch from another in one run::

    JIRA = JiraDataSource(base_url="https://acme.atlassian.net", jql_scope="project = AI")
    TOOLS = [*agent.data_source_tools(JIRA), *agent.data_source_tools(NotionDataSource())]

    # workflow:  await self._runner.run_tool(call_id, TOOLS[0], query="idle timeout")
    # worker:    AgentHarnessPlugin(tools=TOOLS)

The methods run in an activity, on a worker. That is where a source reads its credential, from
the worker's environment, so a token never enters workflow history. The tools are marked
inherently safe because a data source only reads.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness.agent_workflow import activity_tool_defn

_NAME = re.compile(r"[a-z][a-z0-9_]*")

MAX_SEARCH_RESULTS = 50


class SearchResult(BaseModel):
    """One hit of a search. ``id`` is what ``fetch`` takes."""

    id: str
    title: str
    url: str | None = None
    snippet: str = ""
    updated: str | None = Field(default=None, description="When it last changed, ISO 8601.")


class Document(BaseModel):
    """One item of a data source, as text."""

    id: str
    title: str
    text: str
    url: str | None = None
    updated: str | None = Field(default=None, description="When it last changed, ISO 8601.")
    metadata: dict[str, str] = Field(default_factory=dict)


class DataSource:
    """Base class for a data source. Set ``name`` and ``description``, and override
    :meth:`search` and :meth:`fetch`.

    ``name`` goes into the tool names, and ``description`` into their descriptions: say what
    the source holds, in words a model can choose a source by. An instance is built where the
    agent's module is imported, in the workflow too, so its constructor must not read a
    credential or open a connection. Do that in the methods.
    """

    name: str
    description: str

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        """At most ``limit`` results for ``query``, best first."""
        raise NotImplementedError

    async def fetch(self, id: str) -> Document:
        """The item with this id. Raise :func:`not_found` when there is none."""
        raise NotImplementedError


def missing_credential(source: str, variable: str) -> ApplicationError:
    """The error for a credential that the worker's environment does not have."""
    return ApplicationError(
        f"The {source} data source reads {variable}, which is not set on the worker.",
        type="MissingCredential",
        non_retryable=True,
    )


def not_found(source: str, id: str) -> ApplicationError:
    """The error for an id that the source does not have, or may not show."""
    return ApplicationError(
        f"The {source} data source has no item {id!r}.", type="NotFound", non_retryable=True
    )


def data_source_tools(
    source: DataSource, *, activity_config: ActivityConfig | None = None
) -> list[Callable[..., Awaitable[Any]]]:
    """The ``search_<name>`` and ``fetch_<name>`` activity tools of ``source``.

    Give the same list to the agent and to ``AgentHarnessPlugin(tools=...)``."""
    name = getattr(source, "name", None)
    description = getattr(source, "description", None)
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(
            f"{type(source).__name__}: a data source's name must be lowercase letters, digits "
            f"and '_', like 'jira', got {name!r}"
        )
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"data source {name!r} needs a description for the model to read")
    about = description.strip().rstrip(".")

    async def search(query: str, limit: int = 10) -> list[SearchResult]:
        limit = max(1, min(limit, MAX_SEARCH_RESULTS))
        return (await source.search(query, limit))[:limit]

    async def fetch(id: str) -> Document:
        return await source.fetch(id)

    search.__name__ = search.__qualname__ = f"search_{name}"
    search.__doc__ = (
        f"Search {about}. Returns up to `limit` results, best first. Pass a result's `id` to "
        f"fetch_{name} to read the whole item."
    )
    fetch.__name__ = fetch.__qualname__ = f"fetch_{name}"
    fetch.__doc__ = f"Read one item of {about}, by the `id` that search_{name} returned."

    config = activity_config or ActivityConfig(
        start_to_close_timeout=timedelta(seconds=60),
        retry_policy=RetryPolicy(maximum_attempts=3),
    )
    define = activity_tool_defn(inherently_safe=True, activity_config=config)
    return [define(search), define(fetch)]
