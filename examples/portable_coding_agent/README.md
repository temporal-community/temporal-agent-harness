# Portable coding agent

A coding agent whose shell and file edits run inside a **sandbox**, so
model-driven commands cannot touch the host beyond the sandbox. It runs two ways
from one codebase:

- **Durable**, as a Temporal workflow. Sessions survive worker loss and
  redeploys, every sandbox operation is an activity, the sandbox is reached
  through the harness's `temporal_sandbox_client`, and one sandbox is reused for
  the whole session so files persist from one message to the next.
- **Local**, in one process with no Temporal and no server, for the end-user's
  machine.

Both modes are the same `SandboxAgent` (OpenAI Agents SDK); the only difference
is whether the sandbox client is used directly or through Temporal activities.

## Tools and method

The agent works the way a production coding agent works, and its system prompt
tells it to:

- **Read / edit / shell** come from the sandbox: the model lists directories,
  reads files, edits them, and runs commands, all inside the sandbox. It reads a
  file before editing it, and can batch independent reads in one step (the SDK
  runs tool calls in parallel).
- **Plan** with `update_plan`: for multi-step work it lays out the steps, marks
  one in progress, and ticks them off. The plan is the agent's own state (durable
  workflow state in the durable mode), not a sandbox action, so it is an inline
  harness tool rather than a sandbox tool.
- **Search by meaning** with `codebase_search`: an embedding index over the
  project, keyed by content hash so re-indexing only re-embeds changed chunks
  (the merkle-tree idea) and evicts vectors for files that go away. Results carry
  the matched code, not just line ranges. It runs as an activity and reads the
  project on the worker's filesystem (`CODING_AGENT_WORKSPACE`); it complements
  the shell's own `grep`/`find`. See the note below on keeping it consistent with
  the sandbox.
- **Ask the user** with `ask_user`: when the task is ambiguous the agent asks
  rather than guesses, optionally offering a short list of `choices`. In the
  durable mode this is a callback tool (the workflow pauses and an attached client
  answers, at no worker cost); locally it is a terminal prompt.
- **Delegate** with `task`: hand a self-contained sub-task to another instance of
  this agent. In the durable mode that instance is a child workflow (the harness
  `subagent_toolset`); locally it is a nested run. A subagent cannot ask the user
  (nothing answers a child's callback) and delegation depth is capped, so
  subagents do not recurse without bound.
- **Fetch the web** with `web_fetch`: pull a URL's text for documentation or an
  error message the repository does not contain, as an activity-backed tool.
- **Verify**: the prompt has it run the build or the relevant test after a change
  and fix what it broke, rather than declaring success blind. If an edit does not
  apply cleanly it re-reads the region and retries rather than forcing it.
- **Context** stays bounded: the SDK's compaction capability summarizes the
  conversation as it grows.

One consistency note for `codebase_search`: it reads the project on the worker's
disk, while the `docker` sandbox has its own workspace, so out of the box the
agent can search a repo the sandbox is not editing. The results carry the matched
code (not just line ranges), so a mismatch still returns usable content rather
than pointers the sandbox cannot open. To keep search and edits on the same
files, run the `local` backend (tools run on the host, over the same directory
the search reads) or hydrate the sandbox from that directory. A host bind mount
is not a first-class option in this SDK sandbox, which is why a production build
indexes through an indexing service instead.

It is a sibling to [`callback_tools/coding_agent`](../callback_tools/coding_agent),
which keeps its tools on the user's laptop and reasons in the cloud through
callback tools. This one runs the tools in a server-side sandbox, and is the
first example wiring the harness's sandbox seam (`SandboxClientProvider` +
`temporal_sandbox_client`).

## The sandbox

`CODING_AGENT_SANDBOX` picks the backend (both are the OpenAI Agents SDK's own):

- `docker` (default): a throwaway container per session. Real isolation; needs a
  Docker daemon. The image is `CODING_AGENT_SANDBOX_IMAGE` (default
  `python:3.12-slim`).
- `local`: the unix-local backend. The same tools run directly on the host with
  **no isolation**, for a machine you already trust or where Docker is not
  available.

The default image is deliberately small and ships `grep` / `find` / `sed` /
`awk` (enough for text search); set `CODING_AGENT_SANDBOX_IMAGE` to one that also
has `git`, `ripgrep`, and your language toolchain for richer work.

Isolation is the safety boundary. With the `docker` backend the model's commands
cannot reach the host, so this agent does not gate individual commands. Note that
the harness `ToolApprovalPolicy` gates the harness-side tools (plan, search, ask,
web, delegate), not the SDK's own shell and edit tools, so on the `local` backend
(no isolation) that boundary is gone and you are trusting the model on your host.
Use `local` only on a machine you already trust; per-command approval of the
sandbox's own tools would use the SDK's `needs_approval` and is not wired here.

## Durable mode

```bash
cp ../../.env.example ../../.env.local     # fill in the Temporal profile + OPENAI_API_KEY

just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. FastAPI API + UI on :8000
just worker            # 4. this example's agent + model workers (hosts the sandbox)
```

Then open http://localhost:8000, pick "Portable Coding Agent", and chat.

### Placement, and why it is set up this way

The session's sandbox lives on the worker that created it (with the `docker`
backend, a container on that host) and is reused for the whole conversation. Its
operations (create / resume / exec / read / write / delete) run as activities on
the workflow's own task queue and are dispatched eagerly, so they tend to return
to that worker; the session worker also sets a short
`sticky_queue_schedule_to_start_timeout` so a lost worker is re-dispatched
quickly. Because the sandbox persists across messages, this is a hard affinity
requirement: an operation that lands on a worker without the container cannot
serve it, so run a single session worker (or otherwise keep a session's sandbox
operations on the worker that holds its container) until that affinity is
guaranteed. On a lost worker, the workflow resumes the sandbox from its stored
state, which the `docker` backend can do only on the host that still has the
container. The model call, by contrast, runs on its own task queue
(`portable-coding-agent-model`) because it is the long, provider-bound step.

## Local mode (no server)

```bash
export OPENAI_API_KEY=...
just local "write a script that prints the first 10 fibonacci numbers and run it"
# or directly:
uv run --group examples python -m examples.portable_coding_agent.local_runner \
    --session myproj "add a docstring to the top function in main.py"
```

No Temporal is used on this path; the sandbox client is called directly. A
checkpoint file under `~/.portable-coding-agent/sessions/` keeps the
conversation across restarts.

## Working on an existing project

With the `local` backend, point the agent at a real repo and it edits it in
place:

```bash
export CODING_AGENT_SANDBOX=local
export CODING_AGENT_WORKSPACE=/path/to/your/repo
just local "add a docstring to the top function in main.py"
```

The unix-local backend roots its workspace at `CODING_AGENT_WORKSPACE`, and the
SDK never deletes or clears a caller-provided root, so edits land in your files
and `codebase_search` indexes the same tree. It runs with **no isolation**, so
use it only on a repo and machine you trust.

The `docker` backend keeps an isolated workspace instead: its SDK options expose
no host bind mount, so it cannot edit a repo on your disk directly. To work on a
project under isolation you would seed the container from a directory (the
session supports `hydrate_workspace` / `persist_workspace`) and apply changes
back out of band; that copy-in/copy-out flow is not wired here.

## Large-payload retention

A cloud agent that offloads large conversation snapshots (see
`temporal_agent_harness.utils.large_payload`) accumulates blobs, because neither
offload driver deletes. Reclaim space with the sweep:

```bash
just sweep-payloads     # deletes offloaded payloads older than 7 days
```

Run it from cron or a Temporal Schedule. Size the window longer than your
namespace retention period. See
`temporal_agent_harness/utils/large_payload_gc.py` for the age caveat.

## Status

Verified end to end against the OpenAI API with the `docker` backend.

Durable mode, the sandbox-persistence path specifically: a two-message session
was driven against a dev server. History shows the sandbox created once and
resumed on the second message (not recreated), exactly one container served the
whole session, and a file written on the first message was present in that
container on the second (checked out of band with `docker exec`, not the model's
word). The whole tool surface is registered and a turn completes with no errors.

Local mode: the unix-local backend, rooted at a workspace, read an existing repo
file and wrote a file that landed on the host in that repo, and `client.delete`
left the caller's directory intact. `update_plan`, `codebase_search` (live
embeddings, ranked the right file), `ask_user`, and `task` each ran end to end in
an earlier check.

The full test suite is green (258 passed): the search machinery, the
subagent/ask/delegation policy, sandbox backend selection, and the retention
sweep are unit-tested. CI here does not run the model-backed loop or spin a
container. Two paths rest on the harness's own unit-tested mechanisms rather than
a live run here: the durable `ask_user` callback and a full durable `task`
subagent turn.

Not wired here, by design:

- **Editing a repo under `docker` isolation.** The SDK's Docker options have no
  host bind mount, so in-place editing uses the `local` backend; a seed/apply
  flow for docker is a next step.
- **Per-command approval of the sandbox's own shell/edit tools.** Isolation is
  the boundary; gating individual sandbox commands would use the SDK's
  `needs_approval` integrated with the durable approval flow.
- **`AGENTS.md` ingestion is local-mode only**, where the workspace is the real
  repo; the durable sandbox starts empty.

The durable path needed a harness fix (a separate commit in this PR): the model
activity was validating its input back into the typed item union, which made
pydantic hand the SDK lazy `ValidatorIterator`s for `Iterable`-typed fields that
cannot be re-serialized once detached; forwarding the input as opaque JSON fixes
it.
