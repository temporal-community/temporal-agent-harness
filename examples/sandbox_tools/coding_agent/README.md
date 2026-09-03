# Sandboxed coding agent

A real Gemini **coding agent** whose tools run **inside an isolated Daytona cloud sandbox** — never
on the worker's or the user's machine. Ask it to build an app, add a feature, run tests, or explain
code, and an LLM reasons and calls `bash` / `read` / `write` / `edit` / `grep` / `glob` to do it,
all against a project that lives in the box.

It's the sandboxed sibling of [`examples/callback_tools/coding_agent`](../../callback_tools/coding_agent):
**same six tools, same underlying implementations** — the difference is *where* they run. The
callback agent runs its tools on your laptop (via the OpenCode shim, gated to protect your real
machine); this one runs them as durable activities inside a disposable sandbox, and pairs with a
**live-preview proxy** so you can build a web app in the box and open it in your browser.

## What's shared

The two coding agents duplicate almost nothing — the common pieces live in
[`examples/coding_agent_common`](../../coding_agent_common):

| Module | Role |
| --- | --- |
| `tool_impls.py` | The pure `(root, args) → result` implementations (`bash_exec`/`read_file`/`edit_file`/…). Both agents call these; this agent bakes it into its snapshot image. |
| `todo_tools.py` | `todowrite`/`todoread` — inline workflow tools for the agent's task list. |
| `chat_loop.py` | The Gemini Interactions streaming tool-calling loop. |

This example only supplies what genuinely differs: the tools declared as
`@agent.activity_tool_defn(sandboxed=True)` **`BaseModel`-in/out** tools (`tools.py`), the workflow
config (`sandbox=SANDBOX`), the worker, and the snapshot image.

## Requirements

- `GEMINI_API_KEY` — the agent calls the Gemini Interactions API.
- `DAYTONA_API_KEY` — the tools run on a real Daytona cloud sandbox (also used by the preview proxy).
- `DAYTONA_TARGET` *(optional)* — Daytona region for the preview proxy (e.g. `us`).

All go in the repo-root `.env.local` (see `.env.example`).

## Backend: Daytona

The snapshot is built from `examples/Dockerfile.sandbox-coding-agent`. Unusually, that Dockerfile
lives at **`examples/`**, not in this example dir — because the image bakes in BOTH this example's
`tools.py` *and* the shared `coding_agent_common/tool_impls.py`, and Daytona resolves every `COPY`
source relative to the Dockerfile's own directory, so it must sit at the lowest common ancestor of
those two trees. A consequence: `local_project_root` is `examples/`, so the snapshot's content hash
covers everything under `examples/` — editing an unrelated example changes this snapshot's name, so
re-run `just build-sandbox` before the next run if that happens.

The image installs the harness (with its `sandbox` extra) **from GitHub** (`sandbox-tools` branch)
rather than COPYing the repo source — the tools only need the harness + `remote-box`/Daytona SDK,
demonstrating that a sandboxed tool's deps are its own, independent of the worker's. Built ahead of
time, never at runtime (`SandboxConfig.require_prebuilt`): `just build-sandbox`.

## Approvals

The tools run in a disposable box, so the blast radius is contained — but the mutating tools
(`bash` / `write` / `edit`) are still **gated** (`ToolApprovalPolicy.allow_inherently_safe()`) and
the read-only tools (`read` / `grep` / `glob`) + the plan tools auto-approve, mirroring the callback
agent's UX. Approvals surface in the Svelte UI (`GET /api/status/{session_id}`'s `pending_approvals`,
resolved via `POST /api/approve`).

## Run

```bash
just build-sandbox      # once — builds the Daytona snapshot (needs DAYTONA_API_KEY)
just temporal           # 1. local Temporal dev server
just session-manager    # 2. shared session-manager worker
just server             # 3. FastAPI API + UI on :8000
just worker             # 4. this example's agent worker (needs GEMINI_API_KEY + DAYTONA_API_KEY)
just preview-proxy      # 5. (optional) live-preview proxy on :8080
```

Open `http://localhost:8000`, pick "Sandboxed Coding Agent", and chat — e.g. *"build a hello-world
site and serve it on port 3000"*. Approve the `bash`/`write` calls; the agent scaffolds the project
in `/home/daytona/project`, and (for a web app) writes the launch command to `start.sh` in the
project dir and gives you a preview URL.

## Live preview

`preview_proxy.py` is a small, self-contained aiohttp server (it touches nothing in the harness web
app) that lets you open a server the agent started inside the sandbox:

1. **The snapshot entrypoint (`supervise.sh`)** is a keepalive that watches
   `/home/daytona/project/start.sh` (inside the project dir, so the agent's `write` tool can create
   it) and (re)launches it on every boot — so a woken sandbox re-serves automatically.
2. **The agent** writes the foreground, `0.0.0.0`-bound launch command to `start.sh`, then reads
   `$DAYTONA_SANDBOX_ID` and hands you `http://localhost:8080/s/<sandboxId>/<port>/`.
3. **The proxy** wakes a stopped sandbox on request, fetches a fresh preview token, waits for the
   server to bind, then forwards HTTP + WebSocket/HMR traffic.

### Cost: idle sandboxes stop themselves

Waking a sandbox on a preview hit would otherwise leave it billing compute forever (preview HTTP
traffic doesn't count as activity). So the proxy sets a Daytona **auto-stop interval**
(`PREVIEW_AUTO_STOP_MINUTES`, default `3`) on each sandbox it serves. SDK interactions — including the
agent's own tool calls — count as activity, so an active session never stops mid-turn; an idle
preview stops itself and re-wakes on the next request.

### Caveats (this is a demo, not production)

- **Lifetime is tied to the chat session.** The harness stops the sandbox between turns (a container
  sandbox's pause is stop — disk persists, processes are killed; the supervisor relaunches the server
  on wake) and deletes it when the workflow ends. Once you close the session, the preview 404s.
- **Path-based routing.** Absolute asset URLs (`/style.css`) miss the `/s/<id>/<port>/` prefix — the
  agent is prompted to use relative paths or a `<base href>`.
- **No auth** on the proxy — add your own gate before exposing it anywhere.
