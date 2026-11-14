# Data Sources

A data source gives an agent read access to a system of record, such as an issue tracker or a
wiki. Every source has the same two operations, so a model uses all of them the same way:

- `search_<name>(query, limit)` returns results, each with an `id`, a title and a URL.
- `fetch_<name>(id)` returns one item as text.

```python
from temporal_agent_harness.data_sources import JiraDataSource, NotionDataSource
from temporal_agent_harness.harness import agent

JIRA = JiraDataSource(base_url="https://acme.atlassian.net", jql_scope="project = AI")
NOTION = NotionDataSource()
TOOLS = [*agent.data_source_tools(JIRA), *agent.data_source_tools(NOTION)]

# worker
AgentHarnessPlugin(tools=TOOLS)
```

`TOOLS` are ordinary activity tools. Give them to your AI SDK's adapter like any other tool,
or to `agent.code_mode_tool(TOOLS, ...)` so that one script searches one source and fetches
from another.

- **Each call is an activity.** It is retried, it shows in the event stream as a tool call, and
  its result is in history, so a replay doesn't call the service again.
- **Credentials stay on the worker.** A source reads its token from the worker's environment
  inside the activity. The token is never in workflow history or in an event.
- **Read-only.** The tools are marked `inherently_safe`, so a policy such as
  `ToolApprovalPolicy.allow_inherently_safe()` runs them without an approval.

## Built-in sources

| Source | Worker environment | Scope |
| --- | --- | --- |
| `JiraDataSource(base_url=..., jql_scope=...)` | `JIRA_EMAIL`, `JIRA_API_TOKEN` | `jql_scope`, a JQL clause such as `project = AI`. An issue outside it is not found. |
| `NotionDataSource()` | `NOTION_TOKEN` | The pages that are shared with the token's integration. |

Each takes `name=` and `description=`, so one agent can have two Jira sites or two Notion
workspaces. They use the standard library for HTTP and add no dependency.

For source code, documentation and other files in git, use a mount instead: see
[Code Mode](code-mode.md).

## Write your own

Subclass `agent.DataSource`, set `name` and `description`, and write the two methods. They run
on a worker, so they may call the network:

```python
class Handbook(agent.DataSource):
    name = "handbook"
    description = "The team handbook: on-call, releases and how we work."

    async def search(self, query: str, limit: int) -> list[agent.SearchResult]: ...
    async def fetch(self, id: str) -> agent.Document: ...
```

The description goes into both tools' descriptions. Say what the source holds, in words a model
can choose a source by.

An instance is built where the agent's module is imported, which includes the workflow. So read
a credential and open a connection in the methods, not in `__init__`. Treat `id` as untrusted
input: a model writes it.
