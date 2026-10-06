# Code Mode Virtual Filesystem & Skills

**Status:** Phases 1–3 implemented (`temporal_agent_harness/harness/code_mode/vfs.py`, with changes to `monty_stepper.py`, `driver.py` and `tool.py`; example in `examples/code_mode_memory/`). Grounded in pydantic-monty 1.0.0. Monty behavior cited here is from the [filesystem docs](https://pydantic.dev/docs/monty/concepts/filesystem/) and was checked against the installed 1.0.0 runtime; see the appendix.

---

## 1. Goals

1. Give Code Mode scripts a **virtual filesystem**: mounts at virtual POSIX paths that a script explores with plain `open()` and `pathlib`.
2. Make the storage behind each mount **pluggable** through one async protocol, so a mount can be backed by an activity, a Nexus operation, a child workflow, or anything else workflow code can await.
3. Ship an **in-memory filesystem** the developer can seed, for example with a `skills/` directory.

### Non-goals

- **The worker's local disk.** Workflow tasks are not guaranteed to land on the same worker, and anything a script reads during a step is re-read on replay, so worker-local files can differ between the original run and a replay. Monty's native `MountDir` is not used.
- **State tracking by default.** A mount is agent state only when the developer declares it with `agent.vfs_mount` (§3.5, §3.7); otherwise file operations are visible through tool events (§4.3).
- **A harness-generated skills index.** What a mount contains, and how the model learns about it, is up to the developer (§7).
- Hydrating or caching remote trees in memory. Each remote file operation is one backend call. Optimize later if profiling asks for it.
- Running skill scripts (bash, Node, third-party Python). §8 defines the seam only.
- A POSIX-complete filesystem. The VFS covers the operations Monty surfaces.

---

## 2. How Code Mode runs today

The constraints below shape the design:

- **The sandbox steps inside the workflow task.** `ScriptRun` (`monty_stepper.py`) drives a script with `feed_start` and manual `resume` on Monty's synchronous pool. Durability comes from replay: the script is deterministic given the results of its host calls, and those results are in history.
- **Every stop is a dump/restore.** When the script blocks on host calls, the step dumps the session, returns the worker to the pool, and the driver runs the calls as durable `run_tool` dispatches. The next step restores the dump into a fresh checkout. One script is one feed, and each `run_code` call is a new script.
- **Steps are short.** A step must finish within `STEP_TIME_LIMIT_SECS` (1s), enforced by the pool's `request_timeout` watchdog, under Temporal's 2s deadlock detector. Aborts are recorded with patch markers so replay reproduces them (`driver.py`).
- **OS calls come back to the host.** Under `feed_start`, Monty surfaces every OS call as a `FunctionSnapshot` with `is_os_function`. `_os_call` answers clock and entropy calls from workflow-deterministic sources and leaves everything else unhandled.
- **`@agent.init` is the workflow's sync `__init__`.** It cannot await activities, so anything loaded asynchronously happens later, in async workflow code.

---

## 3. Model

### 3.1 Mounts

A mount (`VFSMount`) is a virtual path plus a backend.

```python
code_mode_tool(
    tools,
    name="run_code",
    mounts=[
        VFSMount("/skills", InMemoryFileSystem(seed=self._load_skills), read_only=True,
                 description="Agent skills. Read each skill's SKILL.md before using it."),
        VFSMount("/workspace", InMemoryFileSystem(max_bytes=5_000_000),
                 description="Scratch space for files you produce."),
        VFSMount("/data", S3FileSystem(bucket="reports"), read_only=True,
                 description="Quarterly reports, one PDF per quarter."),
    ],
)
```

- `path`: absolute, normalized POSIX path. Mount paths may not repeat or nest (`/data` and `/data/raw` together are rejected). Validated when `code_mode_tool` is built.
- `backend`: any `FileSystem` (§3.2), including the harness's `InMemoryFileSystem` (§3.4).
- `read_only`: enforced by the harness before any backend call. A write to a read-only mount raises `PermissionError` in the script.
- `description`: the developer's one line for the generated contract (§5).

Mounts are fixed when the tool is built in `@agent.init`, like its tool set. A mount list that depends on the run comes from the agent's init `data` model.

### 3.2 The `FileSystem` protocol

```python
class FileSystem(Protocol):
    async def stat(self, path: PurePosixPath) -> FileStat | None: ...   # None: no such path
    async def read(self, path: PurePosixPath) -> bytes: ...
    async def list(self, path: PurePosixPath) -> list[str]: ...         # entry names

    # Writable mounts only
    async def write(self, path: PurePosixPath, data: bytes) -> None: ...
    async def mkdir(self, path: PurePosixPath) -> None: ...
    async def delete(self, path: PurePosixPath) -> None: ...
    async def rename(self, src: PurePosixPath, dst: PurePosixPath) -> None: ...
```

- Methods run in workflow context, so their bodies must be workflow-safe: `workflow.execute_activity`, a Nexus operation, a child workflow, or pure computation.
- Paths are relative to the mount, already normalized (§6).
- `FileStat` carries `is_dir`, `size`, and an optional `mtime`. The harness converts it to Monty's `StatResult`.
- A mount without the write methods can only be mounted `read_only=True`, checked at construction.
- Failures surface in the script as the matching built-in exception: `FileNotFoundError`, `IsADirectoryError`, `NotADirectoryError`, `FileExistsError`, `PermissionError`, otherwise `OSError`. A backend that calls an activity, child workflow or Nexus operation raises these as `ApplicationError(type="FileNotFoundError", ...)`, and the driver maps those type names back to the built-in type. The script sees the exception's type and message; Monty's exceptions carry no `errno`.

### 3.3 Operation mapping

The harness maps Monty's file operations onto the protocol, so backend authors never deal with Monty's operation set:

| Monty operation | Backend calls |
|---|---|
| `Path.exists` / `is_file` / `is_dir` | `stat` |
| `Path.is_symlink` | none (always `False`) |
| `Path.stat`, `os.chdir` (which issues `Path.stat`) | `stat` |
| `Path.read_text` / `read_bytes` | `read` (text decoded as UTF-8) |
| `Path.write_text` / `write_bytes` | `write` |
| `Path.append_text` / `append_bytes` | `read`, then `write` |
| `open(path, 'r' / 'rb')` | `stat` (must exist, must not be a directory); returns a `MontyFileHandle` |
| `open(path, 'w' / 'wb')` | `write(b"")` |
| `open(path, 'a' / 'ab')` | `stat`, then `write(b"")` if missing |
| `Path.iterdir` | `list` |
| `Path.mkdir` | `mkdir` (`parents` / `exist_ok` resolved by the harness with `stat`) |
| `Path.unlink` / `rmdir` | `delete` (with a `stat` type check) |
| `Path.rename` | `rename` (both paths must be in the same mount) |
| `Path.resolve` / `absolute` | none (path math) |

Reads and writes through an open file handle arrive as path operations carrying the handle's path, so they follow the same rows. One `open()` reads the whole file once (`read(3)`, `readline()` and `readlines()` are served from it), and each `f.write(...)` arrives as `Path.append_*` after `open` has truncated or created the file. `for line in f` is unsupported by Monty.

### 3.4 `InMemoryFileSystem`

The harness's `FileSystem` over a tree held in workflow memory.

```python
@activity.defn
async def load_skills() -> dict[str, str]:
    root = Path(__file__).parent / "skills"
    return {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file()}

class MyAgent:
    async def _load_skills(self) -> dict[str, str | bytes]:
        return await workflow.execute_activity(
            load_skills, start_to_close_timeout=timedelta(seconds=30)
        )
```

- **Contents live on the filesystem instance**, which lives on the Code Mode tool built in `@agent.init`. They persist across scripts in the conversation. Replay rebuilds them by re-running each script against the same recorded results.
- **`seed`** (optional) is an async function, run in workflow context, returning the initial files as `{mount-relative path: str | bytes}`. The runner calls it once, before the first operation on the mount, so authors never trigger it themselves. A mount that is never touched is never seeded.
- **Seeding from a deploy's files is safe through an activity.** The activity reads the worker's disk once, and its result is recorded in history. On replay Temporal returns the recorded result, so a workflow keeps the files it seeded even after a later deploy changes them, while new workflows seed the new files. Reading those files directly in workflow code (including `__init__` or `importlib.resources`) re-reads them on replay and is unsafe. A seed can equally call Nexus, or return data the client passed in the workflow's `data` input.
- **`max_bytes`** (optional, default unlimited) caps the total stored size, seed included. A write that would exceed it raises `OSError` in the script. The contents are held in workflow memory, so the cap protects the worker. Remote backends take no budget from the harness. Temporal's per-payload limit on each result (a seed's included) still applies, and the large-payload offload (`utils/large_payload.py`) covers it.
- The harness never continues-as-new today. If it ever does, the contents would have to be carried across.

### 3.5 Declaring a mount the console shows

A mount declared on the agent class with `agent.vfs_mount(...)` is agent state, like `agent.state(...)`: published under the attribute name, so the console's files pane shows it. The declaration fixes what is static (path, description, access, filesystem class); `bind(...)` in `@agent.init` supplies what depends on the session and returns the `VFSMount`:

```python
class MyAgent:
    workspace = agent.vfs_mount("/workspace", agent.InMemoryFileSystem,
                                description="Scratch space for files you produce.")
    memory = agent.vfs_mount("/memory", LocalDisk, description="Your memory.")

    @agent.init
    def __init__(self, config: AgentConfig, data: MyData) -> None:
        ...
        code_mode_tool(tools, name="run_code", mounts=[
            self.workspace.bind(seed=None, max_bytes=5_000_000),
            self.memory.bind(LocalDiskConfig(directory=data.memory_dir)),
        ])
```

- Over `InMemoryFileSystem` the state is a `FileTree` (below); over an `ActivityFileSystem` (§3.6) it is a `FileIndex` (§3.7). Either starts with the mount's details: `kind`, `mount`, `description`, `read_only`.
- `bind` takes, for an in-memory mount, a keyword-only `seed` with no default (an async function, or an explicit `None`) and `max_bytes`; for an activity-backed one, the filesystem's own config type and an activity `timeout`. It may use `self` and the init data, and runs before or after the runner is built.
- `bind` writes nothing to the state. Startup publishes each declaration's snapshot and nothing else, so attaching to a new session finishes (a turn-0 patch would leave the attach open). The state changes only as scripts use the mount.
- A handle binds once; a second `bind` raises. Several Code Mode tools may share the bound `VFSMount`. Passing the unbound handle to `code_mode_tool` is refused with a hint to bind it.
- See [`vfs-mount-declarations.md`](vfs-mount-declarations.md) for the alternatives considered.

**`FileTree`.** The in-memory filesystem's files live in the state instead of plain workflow memory.

- `FileTree` holds `files: dict[str, FileEntry]`, keyed by mount-relative path, and `directories: list[str]` (every directory but the mount's root). A `FileEntry` is `content`, `encoding` (`"utf-8"`, or `"base64"` for anything that is not UTF-8 text) and `size`.
- The state is the only copy of the files, so tracking costs no second copy in memory. A seed adds to it.
- Every change commits through one `mutate()` block, so it is one state patch: one per write, mkdir, delete or rename (a directory rename moves its whole subtree in one patch), and one for a whole seed. Keys are escaped as JSON Pointer segments (`/files/out~1report.md`).
- Declaring is a choice because of what it streams: each patch carries the written file whole. Declare a filesystem whose files are worth showing, like a workspace, and build large or unchanging ones (seeded reference material) inline as plain `VFSMount`s.
- `max_bytes` applies the same way.

### 3.6 `ActivityFileSystem`

A backend written as plain async methods that the harness runs as activities:

```python
class LocalDisk(agent.ActivityFileSystem[LocalDiskConfig], name="local-disk"):
    def __init__(self, config: LocalDiskConfig) -> None: ...   # validates the config
    async def stat(self, path) -> FileStat | None: ...
    async def read(self, path, offset=0, length=None) -> bytes: ...
    async def list(self, path) -> list[str]: ...
    # write / mkdir / delete / rename for a writable mount
```

- The base class declares all seven methods; a subclass overrides `stat`, `read` and `list`, and all four write methods or none. Defining the subclass generates one activity per method, named `vfs.<name>.<method>`, plus `vfs.<name>.view`: a stat and a ranged read returning one page (at most 256 KB) with the file's size and mtime. `AgentHarnessPlugin(filesystems=[LocalDisk])` registers them.
- `LocalDisk.backend(config)` is the workflow side: each operation runs the matching activity on the workflow's task queue. Every activity builds a fresh instance from the config it is given, so the config is validated on every call, and `OSError`s become non-retryable `ApplicationError`s named for the built-in.
- The config is untrusted input, because the console's file viewer sends it back (below). A filesystem must accept only what is safe to act on.

### 3.7 Indexing an `ActivityFileSystem` mount

A declared mount over an `ActivityFileSystem` keeps a `FileIndex` state. **It holds only the paths this session's scripts have touched (looked up, listed, read or written), never the whole store**: a file no script has looked at is not in it, and another process's change shows up once a script looks at the path again. It is the mount's tree as the session has seen it, paths and metadata (`is_dir`, `size`, `mtime`, a count of the session's writes) but never contents. `ToolCalls` records each operation that succeeds: a stat adds or drops a path, a listing adds the names it returns and drops the children it no longer does, and writes, mkdirs, deletes and renames apply directly. Only a change publishes a patch.

The index also carries a `source`: the filesystem's name, its config and the task queue. It is recorded in the same patch as the first operation, by the bound mount that holds the config; before that the index has no files to open. The console's files pane sends that source with a path to `POST /api/files/view`, which runs `vfs.<name>.view` as a **standalone activity** (`Client.execute_activity`): no workflow is involved, so viewing a file adds nothing to the agent's history and works after the agent has closed. It needs a server that runs standalone activities (the local dev server does; the time-skipping test server does not, and the route answers 501).

**The content a viewer gets is the file as it is in the store now**, not as it was at the point in the agent's history being viewed: the index replays, the store does not. A `FileTree` is the opposite: it holds the contents, so they replay with the session.

---

## 4. Execution

### 4.1 Routing an OS call

`_os_call` gains a file-operation branch:

1. Find the mount whose path is a whole-segment prefix of the operation's path. No mount: `resume_not_handled()`, which is Monty's default error, as today.
2. Read-only mount and a write operation: resume with `PermissionError`.
3. Otherwise end the step at this `FunctionSnapshot` (§4.2).

Every backend, `InMemoryFileSystem` included, takes the same path. Clock, entropy and sleep calls keep their current handling.

### 4.2 File operations end the step

A file operation is a sync call, so the script cannot continue until it has a value. The step stops right at the call, the same way it stops when a script awaits host calls:

- The step dumps the session and returns the worker.
- The driver runs the operation's backend calls (§3.3) through `run_tool` (§4.3), seeding the mount first if it has not been seeded.
- The next step restores the dump and resumes with `{"return_value": ...}` or `{"exception": ...}`.

Stepper changes:

- `Step` can describe "stopped at one file operation" alongside `awaiting`.
- `ScriptRun.resume` accepts a restored `FunctionSnapshot` as well as a `FutureSnapshot`.
- Each stop is its own `step_no`, so the timed-out and crashed patch markers apply unchanged.

Because no file operation runs inside a step, the VFS does not affect how long a step runs.

### 4.3 Every backend call goes through `run_tool`

Every backend call, and every seed, dispatches through `runner.run_tool`, so it gets the agent's approval policy and `tool_start` / `tool_end` events like any host call:

- The harness generates one inline tool per kind of call: `fs_seed`, `fs_stat`, `fs_read`, `fs_list`, `fs_write`, `fs_mkdir`, `fs_delete`, `fs_rename`.
- Each takes the mount path and the mount-relative path as arguments, so approval criteria and the UI can see what a call touches. The backend itself is an `Injected` argument the driver supplies, so it never appears in tool events.
- `fs_write` carries its data as `content` plus `encoding`: text as UTF-8, anything else as base64, so tool events stay JSON.
- They are treated like any other tool: no tool is marked `inherently_safe`, and the approval policy applies to each as it does to the agent's own tools. Customizing approval for file operations comes later (§10).

### 4.4 Concurrency

At each stop the driver starts every host call the script has made that is not yet running, and keeps them running across steps until a later stop asks for their results. A file operation therefore never holds back a host call the script already made. Calls still running when the script ends, made but never awaited, are cancelled.

What stays sequential is inherent to sync calls: a sync call freezes the whole interpreter, so file operations never overlap each other. Five tasks in an `asyncio.gather` that each read a file read one after another.

---

## 5. Model-facing surface

- The generated type-check stubs declare `open` and the five `OSError` subclasses. Monty's checker otherwise rejects them as unresolved names. With the declarations the check passes, and at run time the names are still Monty's own `open` and exception types.
- The Code Mode contract replaces "no filesystem" with a listing of the mounts: path, read-only or writable, and the developer's `description`. The listing comes from the `VFSMount`s, so it needs none of the filesystem's contents and nothing is read to build it. Scripts with no mounts keep today's contract.

---

## 6. Security

- **Paths arrive normalized.** Monty joins relative paths onto the sandbox's working directory and collapses `.` and `..` before surfacing the call: `../../../etc/passwd` arrives as `/etc/passwd`. Routing matches whole path segments, so `/skills-evil` never matches `/skills`. A path that matches no mount is unhandled.
- **Mode enforcement is the harness's**, before any backend runs.
- **Backends receive mount-relative paths**, and errors returned to the script use virtual paths only.
- **Every backend call is approval-gated** (§4.3). In-memory writes are bounded by `max_bytes`.
- The router is small and security-relevant: test it for traversal, segment-prefix confusion, cross-mount rename, and writes to read-only mounts.

---

## 7. Skills

A skill is a directory with `SKILL.md` (frontmatter with name and description, then instructions) and optional supporting files. The harness adds nothing skill-specific:

- The developer seeds an `InMemoryFileSystem` with their skills, typically from an activity that reads a `skills/` directory (§3.4), and mounts it read-only.
- The mount's `description`, or the developer's system prompt, tells the model where skills are and how to use them. The agent reads `SKILL.md` files when relevant and drills into referenced files with ordinary reads.
- Anything a skill tells the agent to produce goes to a writable mount such as `/workspace`.

---

## 8. Script execution seam (later)

Skills may ship scripts that Monty can read but not run. The seam is an ordinary harness activity tool, `run_skill_script(skill, script, args)`, exposed to Code Mode like any other host function:

- It only accepts scripts that exist inside the named skill's mount.
- It runs wherever the developer's activity runs it (a container, a microVM, the OpenAI Agents sandbox provider).
- Input files travel as activity arguments, and output files come back into a writable mount through the result.
- As a harness tool, it is approval-gated and appears in the tool lifecycle.

---

## 9. Phases

1. **Stepper.** (Done.) Stop a step at a file operation, resume a restored `FunctionSnapshot`, and start pending host calls at every stop.
2. **VFS.** (Done.) `VFSMount`, path routing and mode enforcement, the `FileSystem` protocol and operation mapping, `fs_*` dispatch through `run_tool`, exception mapping, `InMemoryFileSystem` with `seed` and `max_bytes`, the `open` stub, and the mount listing.
3. **Examples.** (Done.) A memory example (`examples/code_mode_memory/`) whose `/skills` mount is seeded from an activity, and whose `/memory` mount is an `ActivityFileSystem` over a directory of the worker's disk; and a remote backend over activities (the end-to-end test's `RemoteFileSystem`).
4. **Script execution.** `run_skill_script` as an activity tool.

---

## 10. Open questions

1. Customizing approval for file operations, e.g. treating reads as safe or setting per-mount policy.
2. Generated `fs_*` tool names: shared across every `code_mode_tool` on an agent, or prefixed with the Code Mode tool's name so approval criteria can tell them apart?
3. Should a backend be able to answer multi-call operations (append, `mkdir(parents=True)`) natively, through optional protocol methods, to save steps?
4. Declaring a mount over a hand-written `FileSystem` (neither `InMemoryFileSystem` nor an `ActivityFileSystem`). `vfs_mount` accepts only those two today: a `FileIndex` needs a `source` the console can read from.

---

## Appendix: Monty 1.0.0 behavior this design relies on

Checked against the installed runtime:

- Under `feed_start`, every file operation surfaces as an OS `FunctionSnapshot`. `resume(...)` answers it directly; mounts and `os=` are only consulted by `resume_auto()`.
- A sync OS `FunctionSnapshot` can be dumped, restored in another worker, and resumed with a value later.
- File operations count against `max_suspensions`, under both `feed_run` and `feed_start`, even though the docs say they "do not count as suspensions". (The harness sets no `max_suspensions` today.) `open(...).read()` is two operations: `open`, then `Path.read_text` on the handle.
- The `request_timeout` watchdog restarts at every OS call answered inside a step: a script interleaving 400 OS calls with computation ran 0.6s under a 0.3s watchdog. File operations end the step instead, so this only affects the clock and entropy calls answered inline.
- Under `type_check=True`, `open` and the `OSError` subclasses are unresolved names unless the stubs declare them. `pathlib` type-checks as is.
- At run time the `OSError` subclasses exist, and an exception the host resumes a file operation with keeps its type and message. It has no `errno` attribute.
- Surfaced paths are absolute, joined onto the working directory, with `.` and `..` collapsed.
- `MountDir` overlay writes do not survive `load_snapshot`, one reason native mounts are out of scope.
