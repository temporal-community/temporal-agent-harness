# Code Mode

Most agents call tools one at a time — a round-trip per call. **Code Mode** hands the model a
single tool that runs a Python *script* over your tools, so one turn can search, filter, branch,
and act across many calls with ordinary control flow and `asyncio.gather` concurrency.

`agent.code_mode_tool(tools, name=...)` takes any list of harness tools and returns one inline
tool. Its generated description tells the model the sandbox contract and every host function's
signature + result shape — derived from your tools, so you never hand-write or maintain it. Hand
the returned tool to your model's tool-calling loop like any other tool.

```python
from temporal_agent_harness.harness import agent

# Any @agent.activity_tool_defn / @agent.tool_defn tools — including a subagent toolset.
run_code = agent.code_mode_tool(
    [search_flights, search_hotels, book_flight, get_trip_summary],
    name="run_travel_code",
)
# The model then writes, e.g., a script like this and calls run_travel_code with it:
#
#     import asyncio
#     async def main():
#         flights, hotels = await asyncio.gather(  # independent calls run concurrently
#             search_flights({"origin": "SFO", "destination": "JFK", "date": "2026-07-01"}),
#             search_hotels({"city": "New York", "check_in": "2026-07-01", "check_out": "2026-07-05"}),
#         )
#         cheapest = min(flights["flights"], key=lambda f: f["price_usd"])
#         return await book_flight({"flight_id": cheapest["flight_id"], "passenger_name": "Ada Lovelace"})
#     asyncio.run(main())
```

- **Durable, gated, and observable per call.** The script runs in a sandbox that the workflow
  steps directly, and replay re-runs it against the recorded host results; each host call is
  dispatched back through the runner as its own durable activity — keeping that tool's approval
  policy and `tool_start`/`tool_end` events. Writing the script is inert; only the host calls act.
  The script's clock, randomness and `asyncio.sleep` come from the workflow (a sleep is a
  durable timer), and a script that computes for longer than a second without awaiting a host
  call is stopped with an error.
- **Failures are exceptions the script can handle.** A host call that fails (an activity error, a
  denied approval) raises at its `await`, so the script can `try`/`except` it and carry on;
  uncaught, it ends the script with an error the model sees.
- **Type-checked before it runs.** Code Mode generates static type-check stubs from your tools'
  signatures, so a wrong argument or an unknown result key comes back as an error to fix rather
  than a bad run.
- **Composes over subagents.** `agent.subagent_toolset(...)` returns a list of tools, so drop it
  straight into `code_mode_tool([...])` — the model's script can drive subagents too.
- **Several per agent.** Give one agent multiple `code_mode_tool`s (distinct `name`s) over
  disjoint or overlapping tool sets.
- **A virtual filesystem, if you want one.** Pass `mounts=` and scripts read and write files with
  plain `open()` and `pathlib`. A mount's backend is any async `FileSystem` your workflow can
  await (an activity, a Nexus operation, a child workflow), the built-in
  `agent.InMemoryFileSystem` (optionally seeded by an activity), or an `agent.ActivityFileSystem`:
  plain async methods the harness runs as activities for you. Every file operation goes through
  the runner as an `fs_*` tool call, keeping the approval policy and tool events.
- **Files you can watch.** The console's **VFS File Mounts** pane shows a mount's tree as the agent works.
  Declare the mount on the agent class with `agent.vfs_mount(...)` and bind it in `@agent.init`.
  Over `InMemoryFileSystem`, its files, contents included, are a `FileTree`
  [agent state](client-ui.md#build-a-ui-in-react-or-svelte), so they replay with the session. Over an
  `ActivityFileSystem`, its tree is a `FileIndex` state: **only the paths this session's scripts
  have touched** (looked up, listed, read or written), never the whole store, and never
  contents. A file opens in a large viewer with the trees beside it; for an
  `ActivityFileSystem` it is read from the store through a standalone activity, without
  touching the agent's workflow. **That content is the file as it is now, not as it was at that
  point in the agent's history**: the tree replays, the store does not.
- **Knowledge bundles.** `agent.okf_bundle_vfs_mount(...)` declares a mount whose files form an
  [Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md)
  (OKF) bundle: markdown files with YAML frontmatter, linked to each other. Pass it to
  `agent.okf_code_mode_tool(...)` for a Code Mode tool that explains OKF to the model and gives
  scripts `okf_concepts()`, `okf_links(id)` and `okf_render(frontmatter, body)`. The console
  draws the whole bundle as a live graph. See [OKF bundles](#okf-bundles) below.

```python
run_code = agent.code_mode_tool(
    tools,
    name="run_code",
    mounts=[
        agent.VFSMount("/docs", agent.InMemoryFileSystem(seed=self._load_docs), read_only=True,
                       description="Product docs, one Markdown file per page."),
        agent.VFSMount("/workspace", agent.InMemoryFileSystem(), description="Write your output here."),
    ],
)
```

To show a mount in the console, declare it on the class instead. The declaration fixes the path,
description, access and filesystem; `bind(...)` supplies what depends on the session, and returns
the `VFSMount`:

```python
class MyAgent:
    workspace = agent.vfs_mount("/workspace", agent.InMemoryFileSystem,
                                description="Write your output here.")
    files = agent.vfs_mount("/files", Bucket, description="The team's files.")

    @agent.init
    def __init__(self, config: AgentConfig, data: MyData) -> None:
        ...
        self._run_code = agent.code_mode_tool(tools, name="run_code", mounts=[
            self.workspace.bind(seed=None),                     # or seed=<async fn>, required
            self.files.bind(BucketConfig(prefix=data.team)),    # the filesystem's own config type
        ])
```

An `ActivityFileSystem` is a config model plus async methods that run on a worker. Treat the
config as untrusted input and validate it in `__init__`: the console's file viewer sends it back.

```python
class BucketConfig(BaseModel):
    prefix: PurePosixPath

class Bucket(agent.ActivityFileSystem[BucketConfig], name="bucket"):
    def __init__(self, config: BucketConfig) -> None: ...   # refuse anything unsafe here
    async def stat(self, path: PurePosixPath) -> agent.FileStat | None: ...
    async def read(self, path: PurePosixPath, offset: int = 0, length: int | None = None) -> bytes: ...
    async def list(self, path: PurePosixPath) -> list[str]: ...
    # write / mkdir / delete / rename, for a writable mount

# workflow:  files = agent.vfs_mount("/files", Bucket, ...); self.files.bind(BucketConfig(...))
# worker:    AgentHarnessPlugin(filesystems=[Bucket])   # registers vfs.bucket.stat, .read, ...
```

Viewing an `ActivityFileSystem` file in the console needs a Temporal server that runs
standalone activities (the local dev server does; the time-skipping test server does not).

#### OKF bundles

Agent memory, notes or any other knowledge can be kept as an OKF bundle over any
`ActivityFileSystem`:

```python
class MyAgent:
    memory = agent.okf_bundle_vfs_mount("/memory", Bucket, description="Your long-term memory.")

    @agent.init
    def __init__(self, config: AgentConfig, data: MyData) -> None:
        ...
        self._run_code = agent.okf_code_mode_tool(
            self.memory.bind(BucketConfig(prefix=data.team)),
            name="run_code",
            tools=tools,       # optional: code_mode_tool's other arguments all work
            mounts=[...],
        )
```

- **The tool** is `code_mode_tool` with the bundle mounted and an OKF section in its
  description: concepts, frontmatter fields, `index.md` and `log.md`, and links.
- **`okf_concepts(prefix="")`** returns every concept's frontmatter in one activity, instead of
  a file read per concept. **`okf_links(id)`** gives a concept's links and backlinks.
  **`okf_render(frontmatter, body)`** returns a concept's text with well-formed YAML and records
  who wrote it (`generated`); scripts write it with `pathlib` like any file.
- **Nothing is rejected.** A file with missing or broken frontmatter is still listed and drawn,
  with the problem shown.
- **The graph:** every `ActivityFileSystem` gets a generated `vfs.<name>.okf_graph` activity.
  The console's **Graph** button on an OKF mount runs it as a standalone activity and draws the
  whole bundle as it is now: concepts colored by type, links as edges, stale, deprecated and
  human-confirmed concepts marked.

The [memory example](../../python/examples/code_mode_memory) keeps a user's memory this way.

A worker that hosts a Code Mode agent needs the `code-mode` extra (which pulls in
[`pydantic-monty`](https://pypi.org/project/pydantic-monty/), the sandbox the scripts run in) and
the durable bodies of any activity-backed host tools, which
[`AgentHarnessPlugin`](../../python/README.md#running-a-worker--one-plugin) registers. Code Mode has no activities of
its own. A worker without the extra fails the workflow task that builds the tool, so Temporal
retries it until a worker with the extra picks it up:

```python
client = await Client.connect(..., plugins=[AgentHarnessPlugin(tools=my_tools)])
worker = Worker(client, task_queue=..., workflows=[MyAgent])
```

See [`python/examples/monty`](../../python/examples/monty) for three agents all built on Code Mode: a no-model script
runner, a conversational agent that writes its own scripts, and a subagent-driven variant.
[`python/examples/code_mode_memory`](../../python/examples/code_mode_memory) shows a Code Mode tool working on a
virtual filesystem: skills seeded into memory, and long-term memory on disk that sessions share.
