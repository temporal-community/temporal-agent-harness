# OKF bundle mounts

**Status:** implemented on `worktree-monty-memory-example`. Section 11 records where the
implementation settled details this design left open or differs from it.

**Read first:** [`vfs-mount-declarations.md`](vfs-mount-declarations.md) (how a mount is declared
on the agent class and bound per instance) and
[`monty-vfs-and-skills.md`](monty-vfs-and-skills.md) §3.5 to §3.7 (`FileTree`, `ActivityFileSystem`,
`FileIndex`, and the console's file viewer).

**External reference:** [Open Knowledge Format v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md)
(Apache-2.0). We implement the parts below ourselves; we don't vendor Google's code.

---

## 1. Goal

Let an agent keep its long-term memory as an **OKF bundle**: a directory of markdown files with
YAML frontmatter, linked to each other with ordinary markdown links. Then the console can draw
that memory as a **knowledge graph**.

The harness provides:

- `agent.okf_bundle_vfs_mount(...)`: a mount declared on the agent class whose files form an OKF
  bundle, over any `ActivityFileSystem` the developer already has.
- `agent.okf_code_mode_tool(...)`: a Code Mode tool preconfigured for one bundle. It adds host
  functions for exploring the bundle and writing well-formed concepts, and explains OKF in the
  tool's contract. It accepts everything `code_mode_tool` does, so extra tools and mounts still
  work.
- A generated `vfs.<name>.okf_graph` activity for every `ActivityFileSystem`, which those host
  functions and the console both use. Developers write nothing for it.
- A graph view in the console's VFS File Mounts pane, laid out with `d3-force`.

Memory policy (what to remember, and how) is not the harness's business: the memory example
carries a skill for it, and anyone can copy it.

## 2. OKF in brief

What we rely on from the spec:

- **Bundle:** a directory tree of `.md` files. `index.md` (a directory listing) and `log.md` (a
  dated change history) are reserved names, allowed at any level. Every other `.md` file is a
  **concept**.
- **Concept:** YAML frontmatter between `---` lines, then a markdown body. Only `type` is
  required. `title`, `description`, `tags`, `status` (`draft` | `stable` | `deprecated`),
  `stale_after`, `generated`, `verified` and `sources` are optional.
- **Concept id:** the file's path in the bundle without `.md`.
- **Links:** standard markdown links between concepts, either file-relative (`../x.md`) or
  bundle-absolute (`/x.md`). A link is an untyped, directed relationship.
- **Trust tier:** derived from `verified`. None is "unverified"; only non-`human:` actors is
  "machine-confirmed"; any `human:<id>` actor is "human-reviewed".
- **Version marker:** a bundle-root `index.md` may carry `okf_version: "0.2"` in frontmatter.
- **Consumers are permissive** (§11): they must not reject a bundle for missing optional
  fields, unknown types, unknown keys, broken links or missing `index.md` files.

**The harness never rejects what an agent writes.** Everything that reads a bundle tolerates
anything it finds. The prompting asks for well-formed concepts and `okf_render` makes them easy
to write, but a file with missing or broken frontmatter is still read, listed and drawn, with
the problem shown.

Fields that fit agent memory especially well: `stale_after` for dated plans ("flying to Denver
on the 14th"), `status: deprecated` for corrections kept for history, `verified` with a
`human:` actor when the user confirms a memory, and `log.md` as an audit trail.

## 3. Declaring a bundle mount

```python
class CodeModeMemoryAgentWorkflow:
    skills = agent.vfs_mount("/skills", SkillsDisk, read_only=True,
                             description="Skills, one folder each; start with its SKILL.md.")
    memory = agent.okf_bundle_vfs_mount(
        "/memory", LocalDisk, description="Your long-term memory of the user."
    )

    @agent.init
    def __init__(self, config: AgentConfig, data: MemoryConfig) -> None:
        ...
        self._code_tool = agent.okf_code_mode_tool(
            self.memory.bind(LocalDiskConfig(directory=data.memory_dir)),
            name="run_code",
            mounts=[self.skills.bind(SkillsConfig())],
        )
```

- `okf_bundle_vfs_mount` takes the same arguments as `vfs_mount` and returns the same kind of
  declaration, with the same `bind`. It stays a class-level declaration because its state must
  be: the runner, codegen and console find published state only on the class.
- **It accepts an `ActivityFileSystem` only.** An in-memory bundle is out of scope.
- **The marker is OKF's own:** the mount's `FileIndex` header gains
  `okf_version: str | None = None`, set to `"0.2"` by this declaration and present in the first
  snapshot. The console shows the graph for a mount with `okf_version` set. There is no separate
  "format" concept, and nothing is written to the state after startup.
- A bound bundle mount is an ordinary `VFSMount` (an `IndexedVFSMount`), so it can also be passed
  to a plain `code_mode_tool`. It then gets no OKF host functions or prompting.

## 4. `okf_code_mode_tool`

```python
def okf_code_mode_tool(
    bundle: VFSMount,                 # a bound okf_bundle_vfs_mount
    *,
    name: str,
    tools: Sequence[Callable[..., Awaitable[Any]]] = (),
    mounts: Sequence[VFSMount] = (),
    injections: Mapping[str, Any] | None = None,
    inherently_safe: bool = True,
    auto_approval_criteria: str | None = None,
) -> Callable[..., Awaitable[str]]: ...
```

It calls `code_mode_tool` with:

- `tools` plus the OKF host functions (4.1);
- `mounts` plus the bundle;
- `injections` plus the bundle's backend, under a reserved injection name (`okf_bundle`). A
  developer injection of that name is an error;
- the contract extended with an OKF section (4.2).

It raises `TypeError` when `bundle` isn't a bound OKF bundle mount. One tool serves one bundle.

### 4.1 Host functions

Every file operation a script makes is its own activity, so reading the frontmatter of 200
memories one file at a time is 200 or more activities. The exploration functions walk the
bundle inside one activity instead.

| Host function | Returns | Runs as |
|---|---|---|
| `okf_concepts(prefix: str = "")` | every concept under `prefix`: id, path, type, title, description, tags, status, trust tier, stale, and a `problem` when its frontmatter is missing or broken | one `vfs.<name>.okf_graph` activity, without links |
| `okf_links(id: str)` | the concept's outgoing links (with `dangling` for targets that don't exist) and its backlinks | one `vfs.<name>.okf_graph` activity, with links |
| `okf_render(frontmatter: dict, body: str)` | the concept's full text: `---`, the frontmatter as YAML, `---`, the body | in the workflow, no activity |

- **Reading and writing stay on the file path.** A concept's body is read with
  `Path(...).read_text()`, and a concept is written with
  `Path(...).write_text(await okf_render(...))`. Every write goes through the mount as usual, so
  it is indexed, shown in the pane, and subject to the approval policy.
- **`okf_render`** serializes with `yaml.safe_dump` (key order kept), so the YAML is always
  well formed. It fills in `generated: {by: <agent>, at: <workflow time, UTC>}` unless the
  frontmatter already has `generated`. `<agent>` is the agent's registered name (its workflow
  type), since the agent is what writes the concept. It never refuses a frontmatter without
  `type`; it renders what it is given.
- `okf_concepts` and `okf_links` are inherently safe (reads); `okf_render` writes nothing, so
  it is too.
- They are harness tools with an `Injected[...]` backend parameter, so they appear in the
  script's stubs with only their model-facing parameters, and publish tool events like any
  host call.

### 4.2 The contract

The tool's generated description gains an OKF section, after the mount listing, which marks the
bundle's mount (``- `/memory` (writable, OKF v0.2 bundle): <description>``). It covers:

- what a concept is, the frontmatter fields and their meaning (`type` required; `title`,
  `description` as one sentence, `tags`, `status`, `stale_after`, `verified`, `sources`);
- the reserved files: an `index.md` per directory, listing its concepts as
  `* [Title](slug.md) - description`, and a dated `log.md` at the root. The agent keeps them up
  to date; the harness never writes them;
- links: file-relative paths to concepts that exist, no self-links, one link per mention;
- how to work: find concepts with `okf_concepts` before reading files, follow `okf_links`, and
  write concepts with `okf_render`.

It says nothing about memory: that is the agent's own skill or system prompt.

## 5. The `okf_graph` activity

Subclassing `ActivityFileSystem` already generates `vfs.<name>.view`, which builds the
developer's filesystem from a config and calls its own methods. The harness also generates
`vfs.<name>.okf_graph`, for **every** `ActivityFileSystem`, and
`AgentHarnessPlugin(filesystems=[...])` registers it with the rest. It runs harness code
against the developer's `list`, `stat` and `read`; no model-written code is involved, so no
sandbox is needed.

```python
@activity.defn(name=activity_name(cls.name, "okf_graph"))
async def okf_graph(call: OKFGraphCall) -> OKFGraph:   # config, prefix, links: bool
    fs = cls(cls.config_type.model_validate(call.config))
    return await build_okf_graph(fs, prefix=call.prefix, links=call.links)
```

### 5.1 What it returns

```python
class OKFConcept(BaseModel):
    id: str                       # concept id: "trips/denver"
    path: str                     # "trips/denver.md"
    type: str                     # "Unknown" when missing
    title: str                    # frontmatter title, else the file's stem
    description: str = ""
    tags: list[str] = []
    status: str = "stable"        # kept as written, even if not one of the three
    trust_tier: Literal["unverified", "machine-confirmed", "human-reviewed"] = "unverified"
    stale: bool = False           # now >= stale_after, at the time of the walk
    size: int
    problem: str | None = None    # "no frontmatter", "frontmatter is not valid YAML", "no type", ...

class OKFLink(BaseModel):
    source: str                   # concept ids
    target: str
    dangling: bool = False        # the target isn't in the bundle (yet)

class OKFGraph(BaseModel):
    concepts: list[OKFConcept]
    links: list[OKFLink]          # empty unless asked for
    truncated: bool               # a limit stopped the walk
    walked_at: float
```

Bodies are not returned. A body is read through the mount (scripts) or `view` (console).

### 5.2 How it walks

- Depth-first from `prefix` (the bundle root by default), through `list` and `stat`.
  Directories and files are sorted, so the same bundle gives the same result.
- Every `.md` file except `index.md` and `log.md` is a concept, **always**: a file whose
  frontmatter is missing or unparseable is still a concept, of type `Unknown`, with `problem`
  set. Nothing is dropped.
- **Without links,** only each file's head is read: the first 8 KB, extended once if the
  frontmatter hasn't closed. **With links,** the whole file is read.
- **Frontmatter:** `yaml.safe_load` of the block between the first two `---` lines. Unknown keys
  are ignored. A `tags` that isn't a list is split on commas. Repeated tags are kept once.
- **`verified`:** a bare mapping counts as a one-element list (OKF §5.2).
- **Links:** markdown links `[text](target)` whose target ends in `.md`, ignoring an `#anchor`.
  Fenced code blocks are skipped. URLs with a scheme are ignored. A target starting with `/` is
  bundle-relative; anything else is relative to the linking file. A target outside the bundle
  is dropped. A self-link is dropped. Duplicate links are merged. A target that isn't a concept
  is kept, marked `dangling`.
- **Failures:** a directory whose `list` fails, or an entry whose `stat` fails, is skipped and
  the walk goes on; a concept whose `read` fails is listed with the problem. Only a rejected
  config fails the walk.
- **Limits:** at most 1,000 concept files, 256 KB read per file, and 8 MB in total. Past any
  limit the walk stops and sets `truncated`. The constants live beside `MAX_VIEW_BYTES`.
- **Dependency:** `pyyaml`, made a direct dependency (it is already in the lockfile,
  transitively). Parsing runs in the activity; `okf_render`'s `safe_dump` runs in the workflow,
  imported through the sandbox's pass-through.

### 5.3 Serving it to the console

`POST /api/files/okf-graph` takes the index's `source` (as `/api/files/view` does) and runs
`vfs.<name>.okf_graph` with links as a standalone activity on the source's task queue. The same
rules as the viewer apply:

- It never touches the agent's workflow.
- The result is **live**: the bundle as it is now, not as of the cursor.
- 501 when the server can't run standalone activities.
- The config is untrusted input; the filesystem's `__init__` still refuses anything unsafe.
  The walk reads the same files `view` can already read, so it exposes nothing new.

## 6. The console

### 6.1 Where the graph lives

- An OKF mount's header in the VFS File Mounts pane gets a **Graph** button.
- It opens a large dialog, like the file viewer: the graph on the left, and the selected
  concept on the right. The right side shows the title, type, description, tags, status, trust
  tier, staleness and any `problem`, then the body as rendered markdown read through `view`,
  then a **Cited by** list from the backlinks.
- The pane's tree stays as it is: only what this session has touched. The graph is the whole
  bundle, live. The graph carries the **Live** chip, and a warning when the cursor is behind the
  live edge, like the file viewer.
- Opening a concept from the graph works even when this session never touched it. Today the
  file viewer only opens paths in the mount's tree, so it learns to open a live path that the
  index doesn't hold, saying that the session hasn't touched it.

### 6.2 Layout and look

- **`d3-force`** (about 20 KB): link, many-body, collision and centering forces. The simulation
  runs outside Svelte's reactive state and commits node positions once per animation frame.
- **Stable as it grows:** a refreshed graph keeps the positions of nodes it already had and
  warm-restarts the simulation, so new memories settle in without the rest jumping. Node ids
  are concept ids. The random source is seeded, so a given bundle lays out the same way.
- **Rendering:** SVG, which is enough at memory scale.
- **Nodes:**
  - colored by `type`, from a palette assigned in order of first appearance; `Unknown` is grey;
  - sized by file size;
  - `deprecated` faded, `draft` dashed outline, `stale` marked, `problem` marked;
  - trust tier as a small badge;
  - ghost nodes for dangling links.
- **Interaction:** drag a node (it reheats the simulation), zoom and pan, click to select,
  search by title, id or tag, and filter by type.
- **When it refreshes:** a walk reads the whole bundle, so it runs only when needed. Opening
  the graph walks the bundle unless the pane already holds a walk taken since the mount's state
  last changed, and a **Reload** button walks it again on demand. Nothing walks while the dialog
  sits open.
- **Untrusted content:** titles, descriptions, tags and problems render as text. Bodies go
  through the same escaping markdown renderer as the file viewer, already covered by tests.

## 7. The memory example

- **`/memory`** becomes `agent.okf_bundle_vfs_mount("/memory", LocalDisk, ...)`, and the agent's
  tool becomes `agent.okf_code_mode_tool(...)`.
- **`/skills`** is mounted from the example's `skills/` directory on disk instead of being
  seeded from an activity. `LocalDisk` only accepts paths inside `memories/`, so the example
  adds a read-only sibling, `SkillsDisk`, rooted at `skills/` and implementing only `stat`,
  `read` and `list`. A filesystem with no write methods can't be written through any activity,
  whatever config is sent. `load_skills` and `activities.py` go away.
  - The trade-off: skills are read live from disk on every operation. The reads are recorded,
    so replay is unaffected, but a running session sees a deploy's skill changes from its next
    read onward, where today it keeps the skills it was seeded with. This is fine for an
    example.
  - `/skills` becomes a `FileIndex` mount, so the pane shows the skill files a session has
    read, and opens them live.
- **The skill** (`skills/memory/SKILL.md`) shrinks to memory policy, since OKF itself is
  explained by the tool. It is written for the script environment and doesn't name the sandbox:
  - **What to remember:** facts about the user, their preferences, plans, people and projects;
    not transcripts; never secrets.
  - **Types:** `Preference`, `Person`, `Plan`, `Project`, `Fact`, or a new descriptive type.
  - **Layout:** one concept per memory; folders such as `people/` or `trips/` once there are
    several of a kind.
  - **Lifecycle:** `stale_after` for anything dated; `status: deprecated` and a link to the
    replacement when a memory is corrected; deleting only when the user asks to forget.
    `verified: {by: "human:user", at: ...}` when the user confirms a memory explicitly.
  - **Recall:** `okf_concepts()` first, then read what looks relevant, and say when a memory is
    stale or deprecated.
  - **Logging:** a `log.md` entry for every save, correction or deletion.
- **README:** explains the bundle, the graph, and that the tree shows what the session touched
  while the graph shows the whole bundle, live.

## 8. Decisions made

- The harness never rejects or rewrites what an agent writes. There are no write checks.
- The harness never writes `index.md` or `log.md`; the agent maintains them.
- No in-memory bundles.
- `okf_graph` is generated for every `ActivityFileSystem`, with no opt-in.
- `generated.by` is the agent's name, filled in by `okf_render`.
- The declaration stays on the class and is bound per instance; `okf_code_mode_tool` takes the
  bound mount and composes `code_mode_tool`.
- The console's graph uses `d3-force`.
- There is no harness-published OKF skill. OKF itself is explained in `okf_code_mode_tool`'s
  contract; memory policy lives in the example's skill.

## 9. Open questions

1. **A replayable graph** of what the session touched, from metadata recorded in the
   `FileIndex` as files are read and written. Deferred: the live graph answers "what is in
   memory", which is the main question.
2. **Search.** A `okf_search(query)` host function over titles, descriptions, tags and bodies
   would save the model reading files to find one. Deferred until `okf_concepts` proves not to
   be enough.
3. **Several bundles in one tool.** One bundle per `okf_code_mode_tool` keeps the host
   functions free of a mount parameter. An agent needing two bundles builds two tools.

## 10. Tests

- **Graph builder,** over an in-process filesystem:
  - concepts and their fields, trust tiers (including a bare `verified` mapping), staleness;
  - files with no frontmatter, invalid YAML, a non-mapping block or no `type` are concepts of
    type `Unknown` with `problem` set; nothing is dropped;
  - links: relative, bundle-absolute, `#anchors`, URLs ignored, links inside code fences
    ignored, out-of-bundle and self-links dropped, duplicates merged, dangling links;
  - head-only reads without links; truncation at each limit; deterministic order; `prefix`.
- **Activity:** `vfs.<name>.okf_graph` is generated and registered for every
  `ActivityFileSystem`, and refuses a bad config as `view` does.
- **Host functions,** in a real workflow (`start_local`): `okf_concepts` and `okf_links` each
  run one activity; `okf_render` fills `generated` with the agent's name and workflow time,
  keeps a given `generated`, and renders a frontmatter without `type` as given; a concept
  written with it appears in the `FileIndex`.
- **`okf_code_mode_tool`:** extra tools and mounts are kept; the contract has the OKF section
  and marks the mount; a non-OKF mount and the reserved injection name are refused.
- **Declaration:** `okf_bundle_vfs_mount` sets `okf_version` in the first snapshot, and startup
  still publishes only snapshots.
- **Route:** `POST /api/files/okf-graph` against a local dev server, and 501 when standalone
  activities are unavailable.
- **UI** (server renders): the Graph button only for OKF mounts; the dialog's Live labelling and
  behind-the-edge warning; the concept panel escapes hostile titles, problems and bodies.
- **Browser:** layout stability when the bundle grows, drag and zoom, opening an untouched
  concept from the graph.
- **Example end-to-end:** a session saves two linked memories with `okf_render`; the graph has
  both concepts and the link; a second session recalls them through `okf_concepts`.

## 11. As implemented

- **Code:** `code_mode/okf.py` (parsing, the walk, `render_concept`), `code_mode/okf_tool.py`
  (`okf_code_mode_tool` and its host functions), `okf_bundle_vfs_mount` in
  `code_mode/mount_decl.py`, the generated activity in `code_mode/activity_fs.py`, and
  `POST /api/files/okf-graph` in `web/file_view.py`. Console: `OKFGraphDialog.svelte`,
  `OKFGraphView.svelte` and `state/okfGraph.ts`.
- **`okf_render` also stamps `verified`.** A `verified` entry given without `at` gets the
  workflow's current time, since a script can't read the clock to fill it in itself.
- **`okf_concepts` returns the file's full VFS path** (`/memory/trips/denver.md`), so a script
  can read it directly; the activity and the console use mount-relative paths.
- **The graph's concept panel reads bodies itself.** Rather than teaching the file viewer to
  open paths the index doesn't hold (6.1), the graph dialog reads the selected concept through
  `view` and renders it there, frontmatter stripped. Moving between concepts uses the panel's
  **Links to** and **Cited by** lists: the markdown renderer turns relative links into `#`, so
  links inside a body don't navigate.
- **Before the mount's first operation** the index has no `source`, so the graph can't be
  walked yet; the dialog says so.
- **Two Code Mode stub fixes, found on the way.** Defaulted parameters were rendered as
  required, so a script calling `okf_concepts()` without `prefix` failed the type check; stubs
  now mark a default with `= ...` (without its value). And pydantic's `JsonValue`, which has no
  stub form, now renders as `Any`; the argument is still validated as JSON on dispatch.
- **No `from __future__ import annotations`** in `okf.py` and `okf_tool.py`: the host
  functions' and models' annotations must be real types for the stubs inside the workflow
  sandbox, where string annotations don't resolve.
- **Palette:** types take theme tokens in an order chosen so neighbors differ in hue, with
  yellow and green last because the stale and confirmed badges use them.
