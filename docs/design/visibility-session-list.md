# Listing sessions from Temporal visibility, without a session manager

> Status: **in progress**. Replaces `SessionManagerWorkflow` with direct starts and visibility
> queries.

## The problem

The server lists sessions by asking one long-running workflow, `session-manager`, which starts
every session as its child and keeps them in a list in its state. That makes every deployment's
session count a property of one workflow: its state, its history and its query responses all
grow with every session ever created, and nothing trims them.

The list is not cheap past the manager either. `GET /api/sessions` today:

1. queries the manager for its registry and for every session it has ever started;
2. runs a visibility query for running workflows of the registered types the manager does not
   know about (`_discover_untracked_sessions`), capped at 200;
3. calls `describe()` on every session, for its status;
4. scans up to a page of history on every session, for the first user message the list shows as
   its title.

Steps 3 and 4 are two RPCs per session per request, whichever source the session came from.

The manager has costs beyond scale:

- **Terminating it terminates every session.** Its children use the default parent close policy,
  `TERMINATE`, and the repo's justfile has a recipe that terminates it.
- **Its registry goes stale.** `/api/agents` reads the registry the manager was started with,
  not the server's own; changing `agents.toml` needs the manager reset
  (`docs/guides/examples.md`).
- **Subagents show up as sessions.** A subagent is a child workflow of a registered agent type,
  so step 2 lists running subagents as discovered sessions.
- **It is one more process.** Every deployment runs a `session-manager` worker beside the server.

## The design

Temporal already indexes every workflow. The server starts sessions itself and lists them with
one visibility query; nothing in the harness holds a list of sessions.

### Starting a session

`POST /api/sessions` starts the agent workflow directly:

```python
await client.start_workflow(
    descriptor.workflow_type,
    args=[config] if data is None else [config, data],
    id=session_id or f"agent-session-{uuid4()}",
    task_queue=descriptor.task_queue,
    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
)
```

The session has no parent. A caller-chosen `session_id` stays idempotent through
`USE_EXISTING`: a running workflow with that id is returned rather than started again, which is
what the chat integrations rely on when a webhook is redelivered. This is now true of any
running workflow with the id, not only those one manager remembers.

A `session_id` whose workflow has **closed** starts a new run under the same id (the default
reuse policy). The manager returned the closed session instead, which left a chat thread whose
session had idled out pointing at a workflow that rejects every message.

The request's `task_queue` override, which the server never set, goes away: a session runs on its
registry entry's queue, so the list can find it there.

### Listing sessions

`GET /api/sessions` is one `list_workflows` call:

```
((WorkflowType = 'A' AND TaskQueue = 'q-a') OR (WorkflowType = 'B' AND TaskQueue = 'q-b'))
AND ParentWorkflowId IS NULL
```

- **The registry names the sessions.** The types and queues come from the server's own
  `agents.toml` registry, so the list holds exactly the agents this server launches.
- **`TaskQueue` keeps prefixed deployments apart.** Under `TASK_QUEUE_PREFIX` every registry
  queue is prefixed, so two isolated copies of the harness in one namespace, running the same
  agent types, list only their own sessions. The manager did this with a prefixed workflow id.
- **`ParentWorkflowId IS NULL` drops subagents.** A session is a workflow nobody else started.
- **Paged.** The query string carries no `ORDER BY` (not every visibility store supports it);
  visibility already returns the newest first.

Each row is built from the listed execution alone: `workflow_id`, `agent_workflow_type`,
`created_at` (start time), `execution_status`, `closed`, the registry entry's `label`, and the
title (below). No per-session RPC remains.

The response becomes a page:

```json
{ "sessions": [ … ], "next_page_token": "base64…" }
```

`GET /api/sessions?page_size=100&page_token=…` reads the next one. `next_page_token` is `null`
on the last page.

`GET /api/sessions/{session_id}` returns one session by id (a `describe()`), for a deep link or
a pinned session that is not on a loaded page. It answers 404 for an id that is not a session of
this server's registry.

### The title: a memo the agent writes

Every agent has a title, which the session list shows. Visibility does not index workflow state,
so the agent workflow writes its title to a memo:

```
agent_kit.title: "<the title, at most 100 characters>"
```

and the list reads it off each row as `title`. A memo needs no setup on the namespace, unlike a
custom search attribute, which Temporal Cloud makes you register first.

**Every memo key the Agent Kit writes starts with `agent_kit.`** (`AGENT_KIT_MEMO_PREFIX`), so a
key the Kit adds later never collides with one an agent author writes, and a reader can tell the
Kit's keys from anyone else's.

**The memo holds the title, not the first message.** Until something sets the title, an agent
takes its first admitted `send_agent_message` as its title, rendered for display. Keeping the
memo's meaning "the title" leaves room for the runner to offer a `set_title` later, so an agent
can retitle itself as a conversation moves on, without changing what the list reads.

Rendering the default title moves from the server's `_display_user_message` into the agent
protocol (`title_from_message` / `titleFromMessage`), in both SDKs: the payload's single string
field when there is exactly one, otherwise `type(key=repr, …)`. The TypeScript SDK renders `repr` with the existing `py-repr.ts`.

Upserting a memo is a workflow command, so a session admitted its first message under older
code fails replay under this one. The repo makes no compatibility promises, and those sessions
are the manager's children, which the new list does not show anyway.

### The agent list

`/api/agents` reads the server's own registry. Changing `agents.toml` takes effect on server
restart, with nothing to reset.

### What goes away

- `SessionManagerWorkflow`, `create_session_manager_worker`, `run_session_manager_worker`, the
  `session-manager` CLI subcommand and its use in `dev`, the `session_manager` attribute of
  `agent_kit_dev`, every justfile `session-manager` recipe and the docs that start one.
- `Session.is_discovered` and the 200-session discovery cap, in the server and the console.
- `label`'s `Session N` counter. A session's label is its agent's.

The registry types (`AgentRegistry`, `AgentDescriptor`, `AgentStart`) move from
`web/session_manager.py` to `web/registry.py`, which loads them.

## The console

- **Paging.** The session list loads the first page and offers older sessions on demand. The
  quick switcher filters the loaded sessions.
- **Archive pruning** needs every session there is; it runs only once the last page is loaded,
  where it used to check the discovery cap.
- **A session not on a loaded page** — a deep link, a pin — is fetched by id.
- **A just-created session** is put in the list from the `POST` response, as now. On
  Elasticsearch-backed visibility (Temporal Cloud) a re-list a moment later may not show it yet,
  so a re-list keeps sessions this console created until the list carries them.

## What is left

A list request costs one visibility call per page, whatever the number of sessions. What bounds
it now is the namespace: its visibility rate limit, and its retention, which decides how long a
closed session stays listable. The manager kept closed sessions forever; discovery showed none.

Visibility on Elasticsearch is eventually consistent, about a second behind. A session started
or titled that recently may be missing from a list or lack its title.
