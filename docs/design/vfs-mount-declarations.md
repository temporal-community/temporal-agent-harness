# Declaring Code Mode mounts on the agent class

**Status:** implemented on `worktree-monty-memory-example`, fixing [the bug](#2-the-bug).
Section 5 records how the questions left open by the design were settled. Sections 1 and 2
describe the branch before this change.

**Read first:** [`monty-vfs-and-skills.md`](monty-vfs-and-skills.md), sections 3.1 to 3.7, for
the virtual filesystem as it stands: `Mount`, the `FileSystem` protocol, `InMemoryFileSystem`,
`FileTree`, `ActivityFileSystem` and `FileIndex`.

---

## 1. Where things stand

The branch adds, on top of `main` (which has #169, the virtual filesystem):

| Piece | Where | What it does |
|---|---|---|
| `ActivityFileSystem[Config]` | `temporal_agent_harness/harness/code_mode/activity_fs.py` | A filesystem written as plain async methods (`stat`, `read`, `list`, and optionally `write`, `mkdir`, `delete`, `rename`). Subclassing generates `vfs.<name>.<method>` activities plus a read-only `vfs.<name>.view`. `X.backend(config)` is the workflow-side backend. |
| `AgentHarnessPlugin(filesystems=[...])` | `temporal_agent_harness/plugin.py` | Registers those activities on a worker. |
| `FileIndex` / `IndexEntry` / `IndexSource` | `code_mode/vfs.py` | Agent state holding a mount's tree as the session has seen it: paths and metadata, never contents. `IndexSource` (filesystem name, config as `dict[str, JsonValue]`, task queue) says where the console can read a file. |
| `Mount(index=...)` | `code_mode/vfs.py` | Opts a mount into a `FileIndex`. `ToolCalls` records every successful `fs_*` call into it (`_Index`). |
| `FileTree` header fields | `code_mode/vfs.py` | `kind`, `mount`, `description`, `read_only` added to the existing in-memory `FileTree` state, so the console can show it. |
| `_describe_tracking` | `code_mode/tool.py` | When `code_mode_tool` is built, fills the header fields of each mount's `FileIndex` / `FileTree` with `mutate()`. **This is the cause of the bug.** |
| `JsonValue` in agent state | `harness/state/base.py` | `_check_annotation` accepts pydantic's `JsonValue`. Drafts and `freeze` already handle nested JSON; only the check was missing. |
| `POST /api/files/view` | `temporal_agent_harness/web/file_view.py`, `web/app.py` | Runs `vfs.<name>.view` as a **standalone activity** (`Client.execute_activity`) with the index's source. Returns one page (at most 256 KB). Never touches the agent's workflow. 501 when the server can't run standalone activities. |
| FILES pane | `ui/src/lib/components/agent/FilesPanel.svelte`, `MountTrees.svelte`, `FileViewerDialog.svelte`, `ui/src/lib/state/fileMounts.ts` | Trees of every tracked mount. Clicking a file opens a large modal (modelled on the flow's node inspector) with the trees beside the file. In-memory files come from state, as of the cursor. Indexed files are fetched live and labelled **Live**, with a warning when the cursor is behind the live edge. |
| Memory example | `examples/code_mode_memory/` | `/skills` (`InMemoryFileSystem`, tracked by a `FileTree`) and `/memory` (`LocalDisk`, an `ActivityFileSystem` over `examples/code_mode_memory/memories/<memory_dir>`, tracked by a `FileIndex`). `examples/code_mode_vfs/` is deleted. |

Tests at the time of writing: the full Python suite passes (866), and the UI suite passes (592,
`svelte-check` clean). Headless-browser checks pass for the files pane, but they don't cover
the bug below.

### How the example wires a tracked mount today

```python
class CodeModeMemoryAgentWorkflow:
    skills = agent.state(agent.FileTree)
    memory = agent.state(agent.FileIndex)

    @agent.init
    def __init__(self, config: AgentConfig, data: MemoryConfig) -> None:
        self._runner = AgentWorkflowRunner(config, ...)          # registers + publishes state
        self._code_tool = agent.code_mode_tool(
            [],
            name="run_code",
            mounts=[
                agent.Mount("/skills",
                            agent.InMemoryFileSystem(seed=self._load_skills, state=self.skills),
                            read_only=True, description="..."),
                agent.Mount("/memory",
                            LocalDisk.backend(LocalDiskConfig(directory=data.memory_dir)),
                            description="...", index=self.memory),
            ],
        )                                                           # then mutates the headers
```

One line declares a state, another builds a mount, and the author wires the two together by
hand. Then `code_mode_tool` writes the mount's details into a state that has already been
published.

---

## 2. The bug

**Symptom:** starting a new session of the memory example in the console leaves the message
box disabled. It shows "Connecting to …" until the page is hard-refreshed.

**Root cause:**

1. Declared state is published when the runner is constructed
   (`AgentWorkflowRunner._register_declared_states`, `harness/agent_workflow.py`): one
   `state_snapshot` per state, stamped `turn_number=0`.
2. `code_mode_tool` runs after the runner exists and calls `_describe_tracking`, which
   `mutate()`s each tracked state's header. Each of those publishes a `state_patch`, also at
   `turn_number=0`.
3. `AgentClient.attach` (`harness/agent_client.py`, `_merged_attach.should_stop`) ends a replay
   only on a `turn_end`, or on a turn-0 **snapshot**: the "registration snapshot" exception,
   added for exactly this hang. A turn-0 **patch** is neither, so a brand-new session's attach
   stream stays open forever.
4. The console clears `creatingSession` only when the first attach goes idle, and that flag
   disables the message box (`agentRun.svelte.ts`, `startNewSession`, and the comment around
   `connecting = false` in `attach`). A reload goes through a path that never sets the flag,
   which is why a hard refresh "fixes" it.

**Confirmed by experiment:** against a local dev server, `/api/attach?from_offset=0` for a plain
agent with no state ends immediately. For an agent with tracked mounts it delivers three
snapshots and two patches, then stays open past the 12 s timeout. The console behaved the same
with a UI built from `main` and with the branch's UI, so the UI is not the cause. The trigger is
an agent whose startup publishes patches.

**Why it only showed up now:** before this branch, the memory example declared no state at all.

---

## 3. Approaches ruled out, and why

### 3.1 Let a turn-0 patch end the replay (rejected)

Changing `should_stop` to accept any turn-0 state event fixes the hang. **The user rejected
it**: startup should publish snapshots and nothing else, so allowing patches before turn 1 makes
a wrong stream acceptable. This change was implemented, then fully reverted. `agent_client.py`
and `tests/harness/test_observable_state.py` match `main` again.

### 3.2 Defer publishing state until `@agent.init` returns (rejected)

The runner would record declared state at construction but publish it only once startup ends.
The user's objection: *"why are we doing any dynamic writing to the mount state after it's
constructed?"* The problem is the after-the-fact write itself, not when it's published.

### 3.3 Keep mount details out of the state (rejected)

`FileIndex` / `FileTree` would hold only the tree, and the static mount description would be
published separately (its own startup event, or part of the agent-interface query). It adds a
second channel, and a query needs a running worker, while the event stream still works for
closed sessions.

### 3.4 Give a declared state its initial value from `@agent.init` (rejected)

`agent.state(...)` would accept an initial value computed during `@agent.init`. A general change
to state declaration, made to serve one case.

### 3.5 Config and seed as class-level functions of the init data (rejected)

`agent.vfs_mount(..., config=lambda data: LocalDiskConfig(...))`, with the runner calling it at
registration. The user wants the per-session parts of a mount to be able to use `self` and the
agent's init data freely: a seed may depend on arbitrary init data, and a class body sees
neither. Hence an explicit per-instance `bind(...)` (section 4).

### 3.6 `bind()` writes the header, so it must run before the runner (rejected)

If `bind()` filled the state's header, it would have to run before `AgentWorkflowRunner(...)`
is built (whose constructor publishes the snapshots), with the runner raising on an unbound
mount. It works, since writes made before the runner exists fold into the first snapshot, but
it imposes an ordering rule on `__init__` for no reason the author can see.

### 3.7 `bind()` publishes the mount's first snapshot (rejected)

The runner would skip declared mounts, and `bind()` would publish each one's snapshot when it
is complete. Startup would still publish only turn-0 snapshots, but `bind()` would still be
writing state, which it has no reason to do (section 4.3).

---

## 4. Design: mounts declared on the class, bound per instance

Tracked state has to be a class attribute so the runner and the generated client types can
discover it (`declared_states(type(agent))`; see `harness/state/decl.py`: "nothing else can
create published state"). Mounts are **declared on the class the same way**, and the
declaration owns the tracking state, so the author never wires a state to a mount by hand. The
parts of a mount that depend on the session (seed, config) are supplied per instance by an
explicit `bind(...)`, which writes nothing.

### 4.1 What an author writes

```python
@agent.defn(name="CodeModeMemoryAgent")
class CodeModeMemoryAgentWorkflow:
    skills = agent.vfs_mount(
        "/skills",
        agent.InMemoryFileSystem,
        read_only=True,
        description="Skills, one folder each; start with its SKILL.md.",
    )
    memory = agent.vfs_mount(
        "/memory",
        LocalDisk,
        description=(
            "Your long-term memory of the user, kept across sessions. Read and "
            "write it only as /skills/memory/SKILL.md describes."
        ),
    )

    @agent.init
    def __init__(self, config: AgentConfig, data: MemoryConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._conversation = InteractionConversation()
        self._code_tool = agent.code_mode_tool(
            [],
            name="run_code",
            mounts=[
                self.skills.bind(seed=self._load_skills),
                self.memory.bind(LocalDiskConfig(directory=data.memory_dir)),
            ],
        )
        ...

    async def _load_skills(self) -> dict[str, str]:
        return await workflow.execute_activity(
            load_skills, start_to_close_timeout=timedelta(seconds=30)
        )
```

`LocalDisk` and `LocalDiskConfig` are unchanged. The order of statements in `__init__` doesn't
matter: `bind()` can run before or after the runner is built.

Variations:

```python
# No seed: has to be said explicitly.
scratch = agent.vfs_mount("/scratch", agent.InMemoryFileSystem, description="Scratch space.")
...
self.scratch.bind(seed=None)

# A seed that depends on init data and self.
self.skills.bind(seed=lambda: self._load_skills_for(data.team))

# Per-instance options go on bind.
self.skills.bind(seed=self._load_skills, max_bytes=2_000_000)
self.memory.bind(LocalDiskConfig(directory=data.memory_dir), timeout=timedelta(seconds=10))

# A mount nobody needs to see: built inline, never tracked or published.
agent.VFSMount("/tmp", agent.InMemoryFileSystem(), description="Throwaway files.")

# Reading the tracked state, e.g. in a handler.
known = self.memory.state.current.entries
```

### 4.2 The public surface

```python
@overload
def vfs_mount(path: str, backend: type[InMemoryFileSystem], *, description: str,
              read_only: bool = False) -> InMemoryVFSMountDecl: ...
@overload
def vfs_mount(path: str, backend: type[ActivityFileSystem[C]], *, description: str,
              read_only: bool = False) -> ActivityVFSMountDecl[C]: ...

class InMemoryVFSMountDecl(StateDecl[FileTree]):
    # From the class: the declaration. From an instance: an UnboundInMemoryVFSMount.
    ...

class UnboundInMemoryVFSMount:
    state: StateRef[FileTree]
    def bind(self, *, seed: Seed | None, max_bytes: int = ...) -> VFSMount: ...

class ActivityVFSMountDecl(StateDecl[FileIndex], Generic[C]): ...

class UnboundActivityVFSMount(Generic[C]):
    state: StateRef[FileIndex]
    def bind(self, config: C, *, timeout: timedelta = timedelta(seconds=30)) -> VFSMount: ...
```

- **Naming.** `agent.vfs_mount(...)` returns a declaration, not a `VFSMount`, the same way
  `agent.state(...)` returns a `StateDecl`. The types make the difference visible: passing
  `self.memory` to `code_mode_tool` is a type error ("expected `VFSMount`, got
  `UnboundActivityVFSMount`"), and the runtime error says to call `.bind(...)`.
- **`Mount` is renamed `VFSMount`.** It hasn't shipped in a release (0.6.0 predates #169).
  It is what `bind()` returns, what `code_mode_tool(mounts=...)` takes, and the way to build an
  untracked mount inline. Its `index` field is removed.
- **Typing.** `bind`'s config type comes from the filesystem's type parameter
  (`ActivityFileSystem[LocalDiskConfig]`), so pyright rejects a config of the wrong type.
  `seed` is keyword-only with no default: forgetting it is a type error, and "no seed" is an
  explicit `seed=None`.
- **Discovery.** Each declaration is a `StateDecl`, so `declared_states`, the runner and codegen
  pick it up unchanged. The state id is the attribute name.
- **One bind per instance.** A second `bind()` on the same handle raises. Several Code Mode tools
  may share the bound `VFSMount`; they record into the same state.

### 4.3 What gets published, and when

- **At startup**, the runner publishes each declaration's state as an ordinary snapshot. Its
  initial value comes from the declaration alone: `kind`, `mount`, `description`, `read_only`,
  and an empty tree. Nothing else is written to it before turn 1.
- **`bind()` writes nothing.** It only assembles the instance's `VFSMount` (backend, seed,
  config, options).
- **During turns**, a `FileTree` is filled by `fs_seed` and by the script's writes, as today. A
  `FileIndex` is filled by `_Index` from each recorded `fs_*` call, and the **first** recorded
  call also sets `FileIndex.source` (filesystem name, config, `workflow.info().task_queue`) in
  the same patch. These are ordinary in-turn patches.

The console's viewer needs `source` to read a file (`web/file_view.py` sends it to the
standalone `vfs.<name>.view` activity), but only for files in the index, and files only enter
the index through a recorded call made with the bound mount, which holds the config. So until a
mount's first operation it has no `source`, and nothing to open. Two sessions bound with
different configs each record their own `source`, since each session's index is its own.

### 4.4 What goes away

- `_describe_tracking` and `_update` in `code_mode/tool.py`.
- `Mount(index=...)` and `InMemoryFileSystem(state=...)`. Tracking comes only from
  `vfs_mount` declarations.
- `agent.state(agent.FileTree)` / `agent.state(agent.FileIndex)` as the way to track a mount.
  (Whether declaring them directly should become an error is open; see 5.3.)
- `validate_mounts`' check that a `FileIndex` backs at most one mount: one declaration produces
  one handle and one bind, so it can't be violated.

### 4.5 Why it fits

- Mount identity (path, description, access, backend kind) is static, and sits where static
  things go: the class body.
- The per-session parts (seed, config) are given where `self` and the init data exist.
- The state-to-mount link can't be miswired, because one declaration produces both.
- Codegen sees the mounts, so a generated client can type them.
- Startup publishes only snapshots, which fixes the bug at its cause.

---

## 5. Questions settled while implementing

1. **Declared but never bound.** Not checked. An unbound declaration publishes its empty
   starting state and nothing else, which is harmless.
2. **The `.state` accessor.** `self.memory.state` is the `StateRef`; the handle itself is not a
   ref.
3. **`agent.state(agent.FileTree)` / `agent.state(agent.FileIndex)`.** Still declarable; nothing
   fills them. Not made an error.
4. **Subagents.** Nothing assumes a top-level agent: a declaration is a `StateDecl`, published by
   whichever runner the agent builds.
5. **The viewer and a missing `source`.** Already handled: `FileViewerDialog` shows a hint for
   an index without one, and before the first operation there is no file to open anyway.
6. **Docs.** `README.md`, `monty-vfs-and-skills.md` (sections 3.1, 3.5, 3.7),
   `examples/code_mode_memory/README.md`, the FILES pane's empty state, and the module
   docstrings.

Also: `vfs_mount` accepts exactly `InMemoryFileSystem` or an `ActivityFileSystem` subclass, and
refuses a writable declaration over a read-only `ActivityFileSystem` when it is declared. The
code lives in `code_mode/mount_decl.py`; `IndexedVFSMount` (in `vfs.py`, not exported) is what an
activity-backed `bind` returns, carrying the index and source for `ToolCalls`.

### Tests

All present: the regression test is
`test_a_new_session_with_a_declared_mount_publishes_only_its_snapshot`
(`tests/harness/test_activity_filesystem.py`, checked to fail when a startup patch is added back),
and the declaration tests are in `tests/harness/test_vfs_mount_decl.py`. The browser check is
still to do.

- A regression test that a brand-new session of an agent with declared mounts publishes **only
  snapshots** before turn 1, and that `AgentClient.attach(from_offset=0)` on it finishes. The
  model is `test_attach_to_a_brand_new_session_delivers_the_snapshot_and_stops`
  (`tests/harness/test_observable_state.py`).
- The first recorded `fs_*` call sets `FileIndex.source` in the same patch as the first entry,
  and later calls don't touch it.
- `bind` typing and errors: wrong config type (pyright), missing `seed`, second `bind()`,
  passing an unbound handle to `code_mode_tool`.
- A browser check of "new session, then type right away" against a dev server (see 7).

---

## 6. Decisions already made (don't relitigate)

- **Mounts are declared on the class with `agent.vfs_mount(...)`** and bound per instance with
  `.bind(...)`, which returns a `VFSMount` and writes no state.
- **`Mount` is renamed `VFSMount`.**
- **Startup publishes snapshots only.** No state patches before turn 1. Nothing writes to a
  mount's state outside a turn.
- **`FileIndex.source` is recorded with the first indexed file**, not at startup or bind.
- **Viewing files never touches the agent's workflow.** No query, no update, no child workflow,
  no cache on the worker. Contents come from a standalone activity per page, through the web
  server.
- **Viewed contents of an activity-backed mount are live, not historical.** The UI and docs must
  say so wherever contents are shown; the FILES viewer does.
- **`FileTree` keeps content tracking for the built-in in-memory filesystem.** It's the only
  way to show those contents, and they replay with the cursor.
- **The filesystem config is untrusted input.** The viewer sends it back, so a filesystem's
  `__init__` must refuse anything unsafe. The example's `LocalDisk` only accepts relative paths
  inside its `memories/` folder.
- **Config is a JSON object (`dict[str, JsonValue]`), nested allowed.** Never a JSON string.
- **The `fs_*` tool events still carry file contents** (write inputs, read outputs). Out of scope
  for now.

## 7. Working constraints for the next session

- **Agree before implementing.** The user wants to discuss designs and agree first, and stops
  work that starts without that.
- **Staging:** never `git add`, `git rm` (including `--cached`) or otherwise touch the index. The
  user stages, sometimes mid-session. Delete files with plain `rm`. The user has already staged
  part of this branch.
- **Typing:** no `Any` unless "anything" is the meaning; JSON as objects, never `str`; paths as
  `PurePosixPath` / `Path`. When a framework check refuses the right type, find out whether the
  limit is real before designing around it (the `JsonValue` case in section 1).
- **UI toolchain:** `ui/node_modules` in the repo is a macOS install. Build and test the UI from a
  scratch copy with its own Linux `pnpm install`, laid out as `<scratch>/ui` beside a `packages`
  symlink to the repo's `packages/`, because `ui/src/lib/api/types.ts` imports
  `../../../../packages/client/src/protocol`. Rebuild `temporal_agent_harness/ui/dist` with any UI
  change (the built bundle is committed). UI unit tests render on the server only, so check
  client-side behaviour in a real browser. Playwright 1.49.1 matches the cached `chromium-1148`.
- **Tests that need standalone activities** use `WorkflowEnvironment.start_local()`; the
  time-skipping test server returns `UNIMPLEMENTED` for them.
- **`ui/tests/tool-identity.test.mjs`** reserves the `--tool` colour for tool calls.
- **Live demo state:** the user runs the memory example from this worktree on their Mac and
  rebuilds `dist/` there, so a server started mid-rebuild may serve JSON at `/`. Demo memories
  live in `examples/code_mode_memory/memories/` (gitignored). `from-skills-folder/` and
  `test_memory/` there are the user's earlier demo files, moved out of `skills/memory/` and the
  example root, and `memories/memories/test_memory` is from a session given
  `memory_dir="memories/test_memory"`.
