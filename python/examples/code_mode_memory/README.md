# Code Mode memory example

An assistant with long-term memory that outlives its session, kept as an
[Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md)
(OKF) bundle: a folder of markdown files with YAML frontmatter, linked to each other with ordinary
markdown links. Its one tool, `run_code`, is a Code Mode tool whose scripts read and write the
mounted folders with plain `open()` and `pathlib`:

```python
class CodeModeMemoryAgentWorkflow:
    skills = agent.vfs_mount("/skills", SkillsDisk, read_only=True,
                             description="Skills, one folder each; start with its SKILL.md.")
    memory = agent.okf_bundle_vfs_mount("/memory", LocalDisk,
                                        description="Your long-term memory of the user, kept across sessions. ...")

    @agent.init
    def __init__(self, config: AgentConfig, data: MemoryConfig) -> None:
        ...
        self._code_tool = agent.okf_code_mode_tool(
            self.memory.bind(LocalDiskConfig(directory=data.memory_dir)),
            name="run_code",
            mounts=[self.skills.bind(SkillsConfig())],
        )
```

- `agent.okf_bundle_vfs_mount(...)` declares `/memory` as an OKF bundle mount on the class. Its
  `bind(...)` in `@agent.init` supplies the session's directory.
- `agent.okf_code_mode_tool(...)` is a Code Mode tool for that bundle. It explains OKF to the
  model in the tool's description, and gives scripts three host functions:
  - `okf_concepts()`: every memory's frontmatter (type, title, description, tags, staleness),
    from one activity rather than a file read per memory;
  - `okf_links(id)`: a memory's links and backlinks;
  - `okf_render(frontmatter, body)`: a memory's text with well-formed frontmatter, recording
    which agent wrote it and when.

  It takes `code_mode_tool`'s other arguments, so `/skills` is added with `mounts=`.
- `/skills` holds a single skill, [`memory`](skills/memory/SKILL.md): what is worth
  remembering, the types to use, and how to recall, save, correct, confirm and forget. The
  system prompt only tells the model to read and follow that skill whenever the user asks it to
  remember something, or asks something that may already be in memory.

## Where memory lives

`/memory` is [`LocalDisk`](local_disk.py), an `ActivityFileSystem`: a config model plus plain
async `stat` / `read` / `list` / `write` / `mkdir` / `delete` / `rename` methods that touch the
disk. The harness runs each method as an activity (`vfs.local-disk.read`, ...) and supplies the
workflow side; the worker registers them with
`AgentHarnessPlugin(filesystems=[LocalDisk, SkillsDisk])`. `/skills` is `SkillsDisk`, the same
kind of filesystem over this example's `skills/` folder, with no write methods.

The directory comes from the session's init data, relative to this example's `memories/` folder
(gitignored):

```python
class MemoryConfig(BaseModel):
    memory_dir: PurePosixPath  # e.g. "alice": examples/code_mode_memory/memories/alice, created if missing
```

`LocalDisk` refuses absolute paths, `..`, and any path that resolves outside its directory.
That matters because its config is untrusted input: the console sends it back to read a file or
draw the graph, so a filesystem must only accept configs that are safe to act on.

Memory isn't in the workflow at all. It stays on disk after the session ends, and **every
session started with the same `memory_dir` shares the same memories**. Each read goes to disk,
so a session sees what another one wrote a moment ago. Each operation is still recorded in
history, so a session replays without touching the disk.

The bundle is plain files, so other OKF tools can open it too: `memories/<memory_dir>` is a
bundle as it stands.

A worker's local disk only works as shared memory when a single worker serves the task queue,
which is fine for a demo. A real deployment keeps the same shape but points the methods at
shared storage such as S3 or GCS.

## Watching the memory

Open the console's **VFS File Mounts** pane to see both mounts as trees, updated as the agent
works. Both are indexed mounts: **each tree shows only the files this session has touched**,
not everything in the folder. A new session's `/memory` tree starts empty even when the
directory is full of memories, and a memory another session writes appears once this session
reads it. Click a file to open it in a large viewer that keeps the trees beside it. The viewer
reads the file from disk through a standalone `vfs.<name>.view` activity, which leaves the
agent's workflow untouched and works after the session has closed. **That content is the file
as it is on disk right now, not as it was at the point in the session you're looking at.**

`/memory` also has a **Graph** button. It draws the **whole bundle**, live: every memory as a
node colored by type, every link between memories as an edge, with stale, deprecated and
human-confirmed memories marked. It comes from the `vfs.local-disk.okf_graph` activity, which
the harness generates for every `ActivityFileSystem`. Click a node to read the memory and see
which memories link to it.

Viewing files and the graph needs a Temporal server that runs standalone activities;
`just temporal` (the local dev server) does.

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
   oat milk, that my sister Ana lives in Denver, and that I'm flying there on the 14th." It
   reads the skill, then writes a concept for each (a `Preference`, a `Person` and a `Plan`
   that expires after the 14th), links the trip to Ana, and updates `index.md` and `log.md`.
   Watch the files appear in the VFS File Mounts pane, and open the Graph.
3. Start a **second** session with `memory_dir` set to `alice` and ask "What coffee should you
   get me?" or "Who am I visiting?". The new session has none of the first conversation, but it
   lists the bundle, reads what's relevant and replies from it.
4. Start a third session with `memory_dir` set to `bob`. It finds nothing.

Every file operation and host function call also shows up in the console's tool timeline under
the `run_code` call that made it. This example skips approvals, since approving every read
would mean a click per file.
