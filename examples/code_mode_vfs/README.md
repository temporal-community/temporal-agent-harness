# Code Mode VFS example

An assistant whose Code Mode scripts work on a **virtual filesystem**. Its one tool, `run_code`,
is a Code Mode tool with no host functions and two mounts:

```python
self._code_tool = agent.code_mode_tool(
    [],
    name="run_code",
    mounts=[
        agent.Mount("/skills", agent.InMemoryFileSystem(seed=self._load_skills), read_only=True,
                    description="Skills, one folder each; start with its SKILL.md."),
        agent.Mount("/workspace", agent.InMemoryFileSystem(max_bytes=1_000_000),
                    description="Your scratch space; write everything you produce here."),
    ],
)
```

The model's scripts read and write those mounts with plain `open()` and `pathlib`:

```python
from pathlib import Path
skill = Path('/skills/expense-report/SKILL.md').read_text()
with open('/workspace/expense-report.md', 'w') as f:
    f.write(report)
```

To give it something worth reading, `/skills` holds a minimal sample of instruction-only skills
from [`skills/`](skills/): folders with a `SKILL.md` and supporting files (`expense-report`,
`meeting-notes`). They are plain files the model reads and follows in its scripts; there is no
skills machinery here, and nothing in them runs.

## How files get into the workflow

Both mounts are `InMemoryFileSystem`s, so their files live in the workflow. `/workspace` starts
empty and keeps what the agent writes for the whole conversation. `/skills` has a `seed` that
awaits the `load_skills` activity ([`activities.py`](activities.py)), which reads the `skills/`
directory on whichever worker runs it. The harness runs the seed once, before the first script
touches `/skills`.

The files are read in an activity, not in workflow code, because the activity's result is
recorded in history. A running agent keeps the files it seeded even after a deploy changes
`skills/`, and replaying it on any worker sees the same files. New conversations pick up the
new files.

## Watching the workspace

`/workspace` is tracked in agent state, which is opt-in:

```python
class CodeModeVfsAgentWorkflow:
    workspace = agent.state(agent.FileTree)
    ...
        agent.Mount("/workspace", agent.InMemoryFileSystem(max_bytes=1_000_000, state=self.workspace))
```

Its files live in the `workspace` state, and every write, mkdir, delete or rename is published
as a state patch. Open the console's **AGENT STATE** pane to watch files appear as the agent
writes them. `/skills` is left untracked: it never changes after the seed, and streaming it
would only repeat what `skills/` already holds.

## What you see

Every file operation runs through the runner as an `fs_*` tool call (`fs_seed`, `fs_stat`,
`fs_read`, `fs_write`, ...), so the console's tool timeline shows each read and write under the
`run_code` call that made it. This example skips approvals, since approving every read would
be a click per file.

## Running it

```bash
cp ../../.env.example ../../.env.local   # first time: Temporal connection + GEMINI_API_KEY
just temporal          # local Temporal dev server (or bring your own)
just session-manager
just server            # UI on http://localhost:8000
just worker
```

Then start a **Code Mode VFS** session and try "Make an expense report from the sample
expenses" or paste a meeting transcript and ask for notes.
