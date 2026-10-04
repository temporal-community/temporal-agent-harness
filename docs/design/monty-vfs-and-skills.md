# Code Mode Virtual Filesystem & Skills

**Status:** Design, grounded in the harness's Code Mode implementation (`temporal_agent_harness/harness/code_mode/`) and pydantic-monty 1.0.0. Monty behavior cited here is from the [filesystem docs](https://pydantic.dev/docs/monty/concepts/filesystem/) and was checked against the installed 1.0.0 runtime; see the appendix.

---

## 1. Goals

1. Give Code Mode scripts a **virtual filesystem**: mounts at virtual POSIX paths that a script explores with plain `open()` and `pathlib`.
2. Make the storage behind each mount **pluggable** through one async protocol, so a mount can be backed by an activity, a Nexus operation, a child workflow, or anything else workflow code can await.
3. Ship an **in-memory filesystem** that lives in agent state, so the UI can show a session's workspace.
4. Build **skills** on top: a read-only mount plus a discovery index.

### Non-goals

- **The worker's local disk.** Workflow tasks are not guaranteed to land on the same worker, and anything a script reads during a step is re-read on replay, so worker-local files can differ between the original run and a replay. Monty's native `MountDir` is not used.
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
- **The harness does not own the system prompt.** Agent authors call their model SDK directly. The model-facing text the harness generates is tool descriptions, including the Code Mode contract (`tool.py`).

---

## 3. Model

### 3.1 Mounts

A mount is a virtual path plus a backend.

```python
code_mode_tool(
    tools,
    name="run_code",
    mounts=[
        Mount("/workspace", InMemoryFileSystem(self.workspace, max_bytes=5_000_000),
              description="Scratch space for files you produce."),
        Mount("/data", S3FileSystem(bucket="reports"), read_only=True,
              description="Quarterly reports, one PDF per quarter."),
    ],
)
```

- `path`: absolute, normalized POSIX path. Mount paths may not repeat or nest (`/data` and `/data/raw` together are rejected). Validated when `code_mode_tool` is built.
- `backend`: an `InMemoryFileSystem` or any `FileSystem` (§3.2).
- `read_only`: enforced by the harness before any backend call. A write to a read-only mount raises `PermissionError` in the script.
- `description`: one line for the generated contract (§5).

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
- Failures surface in the script as the matching built-in exception: `FileNotFoundError`, `IsADirectoryError`, `NotADirectoryError`, `FileExistsError`, `PermissionError`, otherwise `OSError`. A backend that calls an activity raises these as `ApplicationError(type="FileNotFoundError", ...)`, so the driver maps those type names back to the built-in type. Today `_script_outcome` would turn them into a plain `Exception`.

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

Reads and writes through an open file handle arrive as `Path.read_*` / `Path.write_*` calls carrying the handle's path, so they follow the same rows. `for line in f` is unsupported by Monty.

### 3.4 `InMemoryFileSystem`

The harness's in-memory filesystem, backed by an `agent.state` the author declares:

```python
class MyAgent:
    workspace = agent.state(Workspace)          # harness-provided HarnessState model

    @agent.init
    async def init(self, config: AgentConfig) -> None:
        self._run_code = agent.code_mode_tool(
            tools, name="run_code",
            mounts=[Mount("/workspace", InMemoryFileSystem(self.workspace))],
        )
```

- `Workspace` holds files and directories keyed by mount-relative path. Text files are stored as text. Binary files are stored as base64, since state is JSON.
- Every write commits through `StateRef.mutate()`, which publishes a JSON-patch event, so the UI sees the workspace change as the script runs.
- Contents persist across scripts in the conversation, because the state outlives each feed. Replay rebuilds them by re-running each script against the same host-call results.
- Initial contents must be deterministic: the state's initial value, or what `@agent.init` sets from its `data` input or an activity result.
- `max_bytes` (optional, default unlimited) caps the total stored size. A write that would exceed it raises `OSError` in the script. The contents are held in workflow memory and streamed as state, so the cap protects the worker. Remote backends take no budget from the harness. Temporal's per-payload limit on each read result still applies, and the large-payload offload (`utils/large_payload.py`) covers it.

---

## 4. Execution

### 4.1 Routing an OS call

`_os_call` gains a file-operation branch:

1. Find the mount whose path is a whole-segment prefix of the operation's path. No mount: `resume_not_handled()`, which is Monty's default error, as today.
2. Read-only mount and a write operation: resume with `PermissionError`.
3. `InMemoryFileSystem`: answer inline, inside the step, and keep running. No step ends and nothing goes through `run_tool`.
4. Any other backend: end the step at this `FunctionSnapshot` (§4.2).

Clock, entropy and sleep calls keep their current handling.

### 4.2 Backend operations end the step

A file operation is a sync call, so the script cannot continue until it has a value. The step stops right at the call, the same way it stops when a script awaits host calls:

- The step dumps the session and returns the worker.
- The driver dispatches the backend operation through `run_tool` (§4.3) and awaits it.
- The next step restores the dump and resumes with `{"return_value": ...}` or `{"exception": ...}`. Resuming may take several backend calls first, for operations that map to more than one (§3.3).

Stepper changes:

- `Step` can describe "stopped at one file operation" alongside `awaiting`.
- `ScriptRun.resume` accepts a restored `FunctionSnapshot` as well as a `FutureSnapshot`.
- Each stop is its own `step_no`, so the timed-out and crashed patch markers apply unchanged.

### 4.3 Approvals and tool lifecycle

Backend operations dispatch through `runner.run_tool`, so they get the agent's approval policy and `tool_start` / `tool_end` events like any host call:

- The harness generates one inline tool per kind of operation: `fs_stat`, `fs_read`, `fs_list` (`inherently_safe=True`) and `fs_write`, `fs_mkdir`, `fs_delete`, `fs_rename` (governed by the approval policy).
- Each takes the mount path and the mount-relative path as arguments, so approval criteria and the UI can see what a call touches.
- `InMemoryFileSystem` operations do not go through `run_tool`. They change only the session's own state, which the UI already sees through state patches.

### 4.4 Concurrency

At each stop the driver starts every host call the script has made that is not yet running, and keeps them running across steps until a later stop asks for their results. A file operation therefore never holds back a host call the script already made.

What stays sequential is inherent to sync calls: a sync call freezes the whole interpreter, so file operations never overlap each other. Five tasks in an `asyncio.gather` that each read a remote file read one after another.

### 4.5 Bounding a step

Monty runs host-side work between turns, outside `request_timeout`, and the watchdog restarts at every OS call. A script that interleaves many inline `InMemoryFileSystem` operations with computation can keep one step running past the 1s limit, and past the 2s deadlock detector. (Clock calls already allow this; a VFS makes it common.)

`ScriptRun._drive` therefore tracks the step's cumulative wall time across OS calls and aborts the step past `STEP_TIME_LIMIT_SECS` (the replay backstop on replay). The abort takes the existing `timed_out` path, so it is recorded with a patch marker and replayed without re-running the step.

---

## 5. Model-facing surface

- The generated type-check stubs declare `open`. Monty's checker otherwise rejects `open` as an unresolved name. With a stub declaration the check passes, and at run time the call still reaches Monty's built-in `open` OS operation.
- The Code Mode contract replaces "no filesystem" with a generated listing of the mounts: path, read-only or writable, and description. The listing is generated from the `Mount`s, so it cannot drift from them. Scripts with no mounts keep today's contract.

---

## 6. Security

- **Paths arrive normalized.** Monty joins relative paths onto the sandbox's working directory and collapses `.` and `..` before surfacing the call: `../../../etc/passwd` arrives as `/etc/passwd`. Routing matches whole path segments, so `/skills-evil` never matches `/skills`. A path that matches no mount is unhandled.
- **Mode enforcement is the harness's**, before any backend runs.
- **Backends receive mount-relative paths**, and errors returned to the script use virtual paths only.
- **Writes to remote backends are approval-gated** (§4.3). In-memory writes are bounded by `max_bytes`.
- The router is small and security-relevant: test it for traversal, segment-prefix confusion, cross-mount rename, and writes to read-only mounts.

---

## 7. Skills

A skill is a directory with `SKILL.md` (frontmatter with name and description, then instructions) and optional supporting files.

- **Mount:** skills appear read-only under `/skills/<name>/`. v1 serves them from a read-only `InMemoryFileSystem` the developer fills, for example from an activity in `@agent.init`. A remote `FileSystem` works too, once the index can be built from it.
- **Discovery index:** the harness parses each `SKILL.md` frontmatter and adds the names and descriptions to the Code Mode tool description, next to the mount listing. The agent reads `/skills/<name>/SKILL.md` when relevant and drills into referenced files with ordinary reads. The harness also exposes the index as a string, so authors can put it in their own system prompt instead.
- **Workspace:** anything a skill tells the agent to produce goes to `/workspace`.
- Changing the skill set mid-conversation changes the tool description, which invalidates prompt caching from that point.

---

## 8. Script execution seam (later)

Skills may ship scripts that Monty can read but not run. The seam is an ordinary harness activity tool, `run_skill_script(skill, script, args)`, exposed to Code Mode like any other host function:

- It only accepts scripts that exist inside the named skill's mount.
- It runs wherever the developer's activity runs it (a container, a microVM, the OpenAI Agents sandbox provider).
- Input files from `/workspace` travel as activity arguments, and output files come back into `/workspace` through the result.
- As a harness tool, it is approval-gated and appears in the tool lifecycle.

---

## 9. Phases

1. **Stepper.** Stop a step at a file operation, resume a restored `FunctionSnapshot`, start pending host calls at every stop, and enforce the cumulative step deadline.
2. **In-memory VFS.** `Mount`, path routing and mode enforcement, `InMemoryFileSystem` with `Workspace` state and `max_bytes`, the `open` stub, the generated mount listing.
3. **Pluggable backends.** The `FileSystem` protocol, the operation mapping, `fs_*` tool dispatch through `run_tool`, and exception mapping. Ship one example backend over an activity.
4. **Skills v1.** Skill loading into a read-only mount, frontmatter parsing, the discovery index.
5. **Script execution.** `run_skill_script` as an activity tool.

---

## 10. Open questions

1. Generated `fs_*` tool names: shared across every `code_mode_tool` on an agent, or prefixed with the Code Mode tool's name so approval criteria can tell them apart?
2. `Workspace` state shape: a flat path-to-entry map is simplest for patches. Does the UI want a tree instead?
3. Should a backend be able to answer multi-call operations (append, `mkdir(parents=True)`) natively, through optional protocol methods, to save steps?

---

## Appendix: Monty 1.0.0 behavior this design relies on

Checked against the installed runtime:

- Under `feed_start`, every file operation surfaces as an OS `FunctionSnapshot`. `resume(...)` answers it directly; mounts and `os=` are only consulted by `resume_auto()`.
- A sync OS `FunctionSnapshot` can be dumped, restored in another worker, and resumed with a value later.
- File operations count against `max_suspensions`, under both `feed_run` and `feed_start`, even though the docs say they "do not count as suspensions". (The harness sets no `max_suspensions` today.) `open(...).read()` is two operations: `open`, then `Path.read_text` on the handle.
- The `request_timeout` watchdog restarts at every OS call: a script interleaving 400 OS calls with computation ran 0.6s under a 0.3s watchdog.
- Under `type_check=True`, `open` is an unresolved name unless the stubs declare it. `pathlib` type-checks as is.
- Surfaced paths are absolute, joined onto the working directory, with `.` and `..` collapsed.
- `MountDir` overlay writes do not survive `load_snapshot`, one reason native mounts are out of scope.
