# Portable coding agent

A coding agent whose shell and file edits run inside a **sandbox**, so
model-driven commands cannot touch the host beyond the sandbox. It runs two ways
from one codebase:

- **Durable**, as a Temporal workflow. Sessions survive worker loss and
  redeploys, every sandbox operation is an activity, and the sandbox is reached
  through the harness's `temporal_sandbox_client`.
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
  (the merkle-tree idea). It complements plain text search. It runs as an
  activity and reads the project on the worker's filesystem
  (`CODING_AGENT_WORKSPACE`); see the note below on keeping that consistent with
  the sandbox.
- **Ask the user** with `ask_user`: when the task is ambiguous the agent asks
  rather than guesses. In the durable mode this is a callback tool (the workflow
  pauses and an attached client answers, at no worker cost); locally it is a
  terminal prompt.
- **Delegate** with `task`: hand a self-contained sub-task to another instance of
  this agent. In the durable mode that instance is a child workflow (the harness
  `subagent_toolset`); locally it is a nested run.
- **Verify**: the prompt has it run the build or the relevant test after a change
  and fix what it broke, rather than declaring success blind.
- **Context** stays bounded: the SDK's compaction capability summarizes the
  conversation as it grows.

One consistency note for `codebase_search`: it reads the project on the worker's
disk, while the `docker` sandbox has its own workspace, so out of the box the
agent can search a repo the sandbox is not editing. To keep search and edits on
the same files, run the `local` backend (tools run on the host, over the same
directory the search reads) or hydrate the sandbox from that directory. A host
bind mount is not a first-class option in this SDK sandbox, which is why a
production build indexes through an indexing service instead.

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

The isolation is the safety boundary, so this agent does not gate individual
commands (unlike the callback coding agent, which prompts for approval because
its tools run on the user's own machine).

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

A session's sandbox lives on the worker that created it (with the `docker`
backend, a container on that host). Its sandbox operations
(create / exec / read / write / delete) run as activities on the workflow's own
task queue and are dispatched eagerly, so they tend to return to that worker; the
session worker also sets a short `sticky_queue_schedule_to_start_timeout` so a
lost worker is re-dispatched quickly. In a pool of workers this is a real
affinity requirement: a sandbox operation that lands on a worker without the
session's container cannot serve it. Run a single session worker, or keep sandbox
operations local to the worker holding the container, until that affinity is
guaranteed. The model call, by contrast, runs on its own task queue
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

The sandbox starts empty and is created fresh per session, so out of the box the
agent builds and runs code inside the sandbox rather than editing a repo on your
disk. To work on existing code, mount it into the sandbox: the Docker backend
supports volume mounts, so a project directory can be mounted read-write and the
agent edits it in place. Wiring a host mount through the run config is the
natural next step for this example and is not done here yet.

## What a fuller build adds

Semantic search, ask-user, and subagent delegation are wired (see Tools above).
A production coding agent typically adds one more, which fits the same shape:

- **Web search / fetch**: for docs and error messages the repository does not
  contain, as an activity-backed tool.

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

Both modes were run end to end against the OpenAI API with the `docker` backend.
Local mode: a `SandboxAgent` created a file and ran it inside a container,
returning the program's output; `update_plan`, `codebase_search` (live
embeddings, ranked the right file), `ask_user` (answered from the terminal), and
`task` (delegated a sub-task and used its result) each ran. Durable mode: a turn
completed through the harness with no errors, the history shows the sandbox
operations on the session queue and the model call on its own queue, and the
workers register all four tool families (sandbox, plan, search activity, ask
callback, `task` subagent). The durable ask-callback and subagent turns run on
the harness's own callback and `subagent_toolset` mechanisms; a full durable
run of each is not exercised here.

The durable path needed a harness fix (a separate commit in this PR): the model
activity was validating its input back into the typed item union, which made
pydantic hand the SDK lazy `ValidatorIterator`s for `Iterable`-typed fields that
cannot be re-serialized once detached; forwarding the input as opaque JSON fixes
it. Sandbox backend selection and the retention sweep are unit-tested (`tests/`);
CI here does not run the model-backed loop or spin a container.
