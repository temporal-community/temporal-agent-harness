# Code Mode memory example

An assistant with long-term memory that outlives its session. Its one tool, `run_code`, is a
Code Mode tool whose scripts read and write mounted filesystems with plain `open()` and
`pathlib`:

```python
class CodeModeMemoryAgentWorkflow:
    skills = agent.vfs_mount("/skills", agent.InMemoryFileSystem, read_only=True,
                             description="Skills, one folder each; start with its SKILL.md.")
    memory = agent.vfs_mount("/memory", LocalDisk,
                             description="Your long-term memory of the user, kept across sessions. ...")

    @agent.init
    def __init__(self, config: AgentConfig, data: MemoryConfig) -> None:
        ...
        self._code_tool = agent.code_mode_tool(
            [],
            name="run_code",
            mounts=[
                self.skills.bind(seed=self._load_skills),
                self.memory.bind(LocalDiskConfig(directory=data.memory_dir)),
            ],
        )
```

Each `agent.vfs_mount(...)` declares a mount on the class: its path, description, access and
filesystem, and the state the console shows for it. `bind(...)` in `@agent.init` supplies what
depends on the session: the seed for `/skills`, the config for `/memory`.

`/skills` holds a single skill, [`memory`](skills/memory/SKILL.md), which says how memory is
laid out (a `MEMORY.md` index plus one file per memory) and how to recall, save, correct and
forget. The system prompt doesn't explain memory. It only tells the model to read and follow
that skill whenever the user asks it to remember something, or asks something that may already
be in memory.

## Where memory lives

`/memory` is [`LocalDisk`](local_disk.py), an `ActivityFileSystem`: a config model plus plain
async `stat` / `read` / `list` / `write` / `mkdir` / `delete` / `rename` methods that touch the
disk. The harness runs each method as an activity (`vfs.local-disk.read`, ...) and supplies the
workflow side; the worker registers them with `AgentHarnessPlugin(filesystems=[LocalDisk])`.

The directory comes from the session's init data, relative to this example's `memories/` folder
(gitignored):

```python
class MemoryConfig(BaseModel):
    memory_dir: PurePosixPath  # e.g. "alice": examples/code_mode_memory/memories/alice, created if missing
```

`LocalDisk` refuses absolute paths, `..`, and any path that resolves outside its directory.
That matters because its config is untrusted input: the console's file viewer sends it back to
read a file, so a filesystem must only accept configs that are safe to act on.

Memory isn't in the workflow at all. It stays on disk after the session ends, and **every
session started with the same `memory_dir` shares the same memories**. Each read goes to disk,
so a session sees what another one wrote a moment ago. Each operation is still recorded in
history, so a session replays without touching the disk.

A worker's local disk only works as shared memory when a single worker serves the task queue,
which is fine for a demo. A real deployment keeps the same shape but points the methods at
shared storage such as S3 or GCS.

## Watching the files

Open the console's **FILES** pane to see both mounts as trees, updated as the agent works.
Click a file to open it in a large viewer that keeps the trees beside it:

- `/skills` is a `FileTree`: an in-memory filesystem whose files, contents included, are agent
  state. Opening a file shows it **as it was at the point in the session you're looking at**,
  and scrubbing back replays it.
- `/memory` is a `FileIndex`: **only the files this session has touched**, not everything in
  the memory directory, and paths and sizes only, never contents. Opening a file reads it
  from disk through a standalone `vfs.local-disk.view` activity, which leaves the agent's
  workflow untouched and works after the session has closed. **That content is the file as it
  is on disk right now, not as it was at the point in the session you're looking at.** The tree
  replays; the disk doesn't.

Because the index holds only what this session has touched, a new session's `/memory` tree
starts empty even when the directory is full of memories, and a memory another session writes
appears once this session lists or reads it.

Viewing `/memory` files needs a Temporal server that runs standalone activities; `just temporal`
(the local dev server) does.

## Running it

```bash
cp ../../.env.example ../../.env.local   # first time: Temporal connection + GEMINI_API_KEY
just temporal          # local Temporal dev server (or bring your own)
just session-manager
just server            # UI on http://localhost:8000
just worker
```

## The demo

1. Start a **Code Mode Memory** session. The UI asks for its init data; set `memory_dir` to
   `alice`.
2. Tell it something worth remembering: "Remember that I take my coffee as a flat white with
   oat milk, and that I'm flying to Denver on the 14th." It reads the skill, then writes
   `favorite-coffee.md`, `denver-trip.md` and an index line for each under `/memory`. Watch them
   appear in the FILES pane, and open one.
3. Start a **second** session with `memory_dir` set to `alice` and ask "What coffee should you
   get me?" or "When's my trip?". The new session has none of the first conversation, but it
   reads the skill, finds the answer in `/memory` and replies from it.
4. Start a third session with `memory_dir` set to `bob`. It finds nothing.

Every file operation also shows up in the console's tool timeline as an `fs_*` tool call under
the `run_code` call that made it. This example skips approvals, since approving every read
would mean a click per file.
