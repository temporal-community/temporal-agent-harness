# Sandboxed tool demo

A real, Gemini-backed conversational agent (`workflow.py`) with exactly one tool: `run_bash`
(`tools.py`) — an arbitrary bash command that runs inside an isolated **Daytona** cloud sandbox,
never on the worker's own machine. `run_bash` is a
`@agent.activity_tool_defn(sandboxed=True)` tool, and **every single call requires your explicit
approval before it runs** (`ToolApprovalPolicy.always_require_approvals()` — never
auto-approved, by design, since a bash command can do anything a shell can).

A real interactive worker + UI wiring (`worker.py`, `agents.toml`, `justfile`) is provided.

## Requirements

- `GEMINI_API_KEY` — the agent calls the Gemini Interactions API to converse and decide when to
  call `run_bash`.
- `DAYTONA_API_KEY` — `tools.py`'s `SANDBOX` runs `run_bash` on a real Daytona cloud sandbox.

Both go in the repo-root `.env.local` (see `.env.example`).

## Backend: Daytona

The sandbox image is built from `../../Dockerfile.sandboxed-tool-demo` — **at the repo root, not
in this directory**: remote-box's Daytona backend resolves every Dockerfile `COPY` source
relative to the Dockerfile's own directory (a `daytona_sdk` quirk, not something
`local_project_root` controls), so the Dockerfile has to actually live where
`pyproject.toml`/`uv.lock`/the rest of the project are. That Dockerfile also lists every `COPY`
source explicitly, never `COPY . .` — Daytona's SDK builds its own upload list by parsing the
Dockerfile's `COPY` lines directly (it never runs a real `docker build`), so `.dockerignore` is
never consulted; a bare `COPY . .` would upload `.venv`, `.git`, and `.env.local` (which holds
real API keys) right into the snapshot.

Built ahead of time, never at runtime (`SandboxConfig.require_prebuilt` defaults to `True`):

```python
from temporal_agent_harness.harness.sandbox import build_sandbox
from examples.sandboxed_tool_demo.tools import SANDBOX
build_sandbox(SANDBOX)  # first run takes a while (real image build); cached after that
```

Swap `Daytona(...)` for `remote.Subprocess()` (no API key, no image build — reuses your local
venv directly) or `remote.E2B(...)` in `tools.py`'s `SANDBOX` to run the exact same tool under a
different backend — nothing else in this demo changes, since the tool never chooses its own
backend (`harness/sandbox/`'s whole point).

## The approval gate

`run_bash` is never eligible for auto-approval under any policy this agent uses. Every call:
1. Pauses in-workflow, publishing a `tool_approval_requested` event (visible on the turn stream
   and via `GET /api/status/{session_id}`'s `pending_approvals`).
2. Waits — indefinitely, with no activity timeout consumed — for a human decision.
3. Resolves via `POST /api/approve` (or `AgentClient.approve_tool(tool_id, approved=...)`
   programmatically), publishing `tool_approval_resolved`, then either dispatches the sandboxed
   activity (approved) or reports the denial back to the model (denied) so it can react instead
   of retrying blindly.

See `docs/internal/human-in-the-loop-tool-approvals.md` for the full design.

## One-shot script

```bash
uv sync --extra sandbox   # once — pulls in remote-box (needs Python >= 3.12)
uv run --extra sandbox --group examples python -m examples.sandboxed_tool_demo.demo
```

Builds the sandbox image, starts an ephemeral local Temporal server + worker, asks the model to
list the current directory, auto-approves the resulting `run_bash` call (standing in for the
human a real UI would prompt), prints the model's reply, and tears everything down.

## Interactive (chat through the real UI)

```bash
just build-sandbox      # once — builds the Daytona snapshot (needs DAYTONA_API_KEY)
just temporal            # 1. local Temporal dev server
just session-manager     # 2. shared session-manager worker
just server               # 3. FastAPI API + UI on :8000
just worker                # 4. this example's agent worker (needs GEMINI_API_KEY + DAYTONA_API_KEY)
```

Then open `http://localhost:8000`, pick "Sandboxed Tool Demo", and chat. Ask it to run something
(e.g. "what's in the current directory?" or "what's the Python version?") — it'll explain what
it's about to run, then wait for you to approve before the command actually executes in the
sandbox.
