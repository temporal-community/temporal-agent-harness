# Sandboxes for harness agents, built on the OpenAI Agents SDK sandbox layer

Status: phases 1–4 implemented in `temporal_agent_harness/harness/sandbox/` (tests:
`tests/harness/test_sandbox.py`, against OpenAI's real `unix_local` backend). "Not yet built" notes below mark what
remains.

## Summary

- Give **every** harness agent (Gemini, pydantic-ai, Code Mode/Monty, OpenAI Agents) durable remote sandboxes, using the
  OpenAI Agents SDK's `agents.sandbox` layer as the engine. This means its `BaseSandboxClient`/`SandboxSession` contract,
  the backends that ship with it, manifests, snapshots, and capabilities.
- Expose that layer at full strength rather than behind a narrow handle. Agents get four surfaces over **one
  workflow-owned sandbox**:
  1. **Model-facing tools** from any OpenAI capability (`Shell`, `Filesystem`, `Skills`, … or your own), run the
     way the OpenAI Agents Temporal plugin runs them.
  2. **The real `SandboxSession`** passed to custom activity tools that declare an `Injected[SandboxSession]` parameter.
  3. **A workflow-side session** (`await runner.sandbox()`) for scripted setup and teardown.
  4. **The same session handed to OpenAI `SandboxAgent` runs**, so they stop creating and deleting a box every turn.
- The harness adds what OpenAI's SDK doesn't have, because it lives in-process. Most of these are the good ideas in
  `sandbox-orchestration-harness`:
  - Temporal-durable lifecycle state.
  - Lazy creation.
  - Idle policies between turns.
  - Snapshot retention.
  - Rules that make it safe across many workers.
- **Depend on `openai-agents` through an optional extra. Don't vendor it.** The reasoning and the exit plan are below.
- Most of the plumbing already exists in `temporal_agent_harness/ai_sdks/openai_agents/sandbox/` (867 lines). It just
  moves up a level and grows a lifecycle.

## Why OpenAI's sandbox layer (and not sandbox-orchestration-harness or a Go binding)

| | sandbox-orchestration-harness (Go) | `agents.sandbox` (Python) |
|---|---|---|
| Size | 2,545 lines (1,033 of them provider glue); no SDK tests | 22.3k box layer + 5.75k agent layer + 15.3k backends; ~73k lines of tests |
| Backends | E2B, Daytona, Modal, AgentCore, GKE | Modal, E2B, Runloop, Cloudflare, Vercel, Daytona, Blaxel, plus Docker and local Unix in core |
| Exec | `cmd string`; Daytona drops stderr | timeout/user/shell, PTY + stdin, output capped by tokens |
| Files and workspace | none | read/write, bounded read, `apply_patch`, extract, exposed ports; `Manifest` of files, dirs, git repos, S3/GCS/R2/Azure/Box mounts, users |
| Box lost | unhandled | `resume(state)` reattaches, or rebuilds and restores from a snapshot (fingerprint-checked) |
| Errors | `ErrUnsupported`, otherwise a string | `SandboxError` with `error_code` and a `retryable` flag |
| Secrets | provider env vars only | worker-side client construction; mount credentials redacted from serialized state |
| Lifecycle verbs | Suspend/Resume/Snapshot/DeleteSnapshot, idle auto-suspend, suspend-via-snapshot fallback | start/stop(persist)/shutdown/delete; pause only as a per-provider `pause_on_exit`; no idle timer; no snapshot deletion |

The OpenAI SDK maintains every backend itself under `src/agents/extensions/sandbox/` (`CODEOWNERS`:
`@seratch @rm-openai @openai/sdks-team`). They are not vendor-supplied packages, but they're far more mature than the
Go repo's. The Go repo's genuine advantages are all workflow-side policy, which this design carries over (see
"Lifecycle").

A C binding to the Go providers was spiked and works:
- the shared library is 65 MB stripped;
- ctypes calls release the GIL, and cancellation works;
- Go only sees the environment as it was when the library loaded, so credentials can't be supplied at runtime.

The binding would only save ~1k lines of provider glue, at the cost of per-platform native wheels. Rejected.

## Depend, don't vendor

**What vendoring would take** (measured against `openai-agents` 0.23.1 with an AST import-closure script):

- Importing `agents.sandbox` as-is loads **126 other `agents` modules (~61k lines)**, plus `openai` and `griffe`. Most
  of that comes from `agents/__init__.py`, which imports everything.
- The **box layer** (client, session, manifest and entries, snapshots, errors, utilities, backends; no capabilities,
  memory or runtime) is ~22.3k + ~15.3k lines. It touches the rest of `agents` through only a few thin edges:
  - two limits dataclasses and a constant from `run_config`;
  - redaction helpers from `exceptions`;
  - spans from `tracing`;
  - `apply_diff`/`editor` (459 lines);
  - `_config_coercion`, `logger`, and `_asyncio_tasks`;
  - one Docker image constant in `sandbox/config.py`, which is otherwise memory-feature configuration.

  All of these can be cut. So a box-only copy is **technically feasible: about 38k lines, plus ~1.5k of utilities**.
- The capabilities (model-facing tools) depend on `FunctionTool`/`RunContextWrapper`. Vendoring those means rewriting
  them on harness tool types.

**Why not do it:**
- **Churn.** There have been about 60 `openai-agents` releases since March 2026. The last two minor releases alone have
  ~20 sandbox fixes, mostly security hardening: host-grant validation, symlink and special-file handling during restore,
  recursive-removal grant checks, and rejecting host sources in manifests. `_mount_security.py` alone is 2.3k lines. A
  fork would have to track all of that by hand.
- **Testing.** The ~73k lines of tests wouldn't come with the copy, and that's where the robustness actually lives.
- **Vendor SDK pins.** The backends pin vendor SDKs exactly (`e2b==2.31.0`, `modal==1.4.3`), and every bump would
  become ours.
- **The harness already depends on `openai-agents`** for its OpenAI Agents integration, including the Temporal sandbox
  adapter this design builds on.

**What depending costs:** a Gemini-only user who enables sandboxes also installs `openai`, `mcp`, `websockets`,
`griffelib`, `requests`, and the security floors (`starlette`, `pyjwt`, …). It never calls OpenAI or needs an OpenAI
key. Contain that cost:
- a `sandbox` extra (`openai-agents`, at the same `>=0.22.0` floor as the `openai-agents` extra the lock already
  satisfies; backends with their own SDKs come from `openai-agents[e2b]` and friends);
- core harness modules never import `agents` (the same rule `sandbox-tools` used for remote-box);
- contract tests against `BaseSandboxClient`/`SandboxSession` in CI, run against the latest release.

**Exit plan if the dependency weight becomes a real complaint:**
1. First, propose an upstream split (`openai-agents-sandbox`). The measurements above show the box layer is nearly
   separable already, which makes the case for upstream easy.
2. Failing that, adapt the box layer plus only the backends we use. Keep `LICENSE.openai-agents` and a `THIRD_PARTY.md`
   noting what changed, and pin behavior with golden tests captured from upstream, not with byte-identical copies.

## Architecture

```
 Workflow (deterministic)                                   Worker
 ┌───────────────────────────────────────────────┐          ┌──────────────────────────────────────────────┐
 │ AgentWorkflowRunner(sandbox=SandboxConfig)    │          │ SandboxClientProvider("e2b", E2BSandbox      │
 │   SandboxLifecycle  (owns SandboxSessionState)│─ acts ──▶│   Client(), dependencies={snapshot store})   │
 │     lazy create · idle policy · close         │          │   create/resume/stop/shutdown/delete         │
 │                                               │          │   session cache keyed by session_id          │
 │ runner.sandbox()  → TemporalSandboxSession    │─ acts ──▶│   exec/read/write/pty/... (existing adapter) │
 │ sandbox tools (capabilities, run here)        │─ acts ──▶│   (their exec/read/write/pty calls, above)   │
 │ tool with `session: Injected[SandboxSession]` │─ acts ──▶│   real SandboxSession passed to tool body    │
 │ OpenAI SandboxAgent turn                      │          │                                              │
 │   SandboxRunConfig(session=<runner-owned>)    │          │                                              │
 └───────────────────────────────────────────────┘          └──────────────────────────────────────────────┘
```

**Invariant:** only the workflow changes lifecycle. Only `SandboxSessionState` enters history: a small pydantic model
with a type discriminator (backend id, manifest, snapshot ref, ports). Workspace bytes stay on the worker side or in
the snapshot store. Every activity carries the state, and a worker that has never seen this session resumes it from
that state. That is the existing `SandboxClientProvider._session()` pattern.

## Developer surface

```python
# workflow module — OpenAI SDK types are imported pass-through, like any I/O-capable library
with workflow.unsafe.imports_passed_through():
    from agents.sandbox import Manifest, RemoteSnapshotSpec
    from agents.sandbox.entries import GitRepo
    from agents.extensions.sandbox.e2b import E2BSandboxClientOptions
    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent import Injected
    from temporal_agent_harness.harness.sandbox import (
        Filesystem, IdlePolicy, SandboxConfig, SandboxSession, Shell,  # OpenAI's Shell/Filesystem, tuned
    )

SANDBOX = SandboxConfig(
    client="e2b",                                             # a SandboxClientProvider registered on the worker
    options=E2BSandboxClientOptions(template="my-coding-box"),
    manifest=Manifest(entries={"repo": GitRepo(repo="acme/app", ref="main")}),
    snapshot=RemoteSnapshotSpec(client_dependency_key="snapshots"),   # shared store; see "Durability rules"
    capabilities=[Shell(), Filesystem()],
    idle=IdlePolicy(after=timedelta(minutes=5), action="persist_and_shutdown"),
)

@agent.init
def __init__(self, config: AgentConfig) -> None:
    self._runner = AgentWorkflowRunner(config, stream=WorkflowStream(), approval_policy_default=..., sandbox=SANDBOX)
    # exec_command / write_stdin / view_image / apply_patch, as inline harness tools any model SDK can use
    self._tools = [*self._runner.sandbox_tools(), run_tests]
    # appended to the system prompt: the capabilities' own instruction fragments
    self._instructions = BASE_PROMPT + self._runner.sandbox_instructions()
```

```python
# a custom tool gets the REAL session (full API), resumed worker-side from the workflow's state
@agent.activity_tool_defn(activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=10)))
async def run_tests(session: Injected[SandboxSession], pattern: str) -> str:
    """Run the test suite filtered by `pattern`."""
    r = await session.exec(f"cd /workspace/repo && pytest -q -k {shlex.quote(pattern)}", timeout=600)
    return r.stdout.decode()[-8000:]
```

```python
# workflow-side scripting: every call is an activity (TemporalSandboxSession, already in the repo)
sb = await self._runner.sandbox()          # create or resume as needed
await sb.exec("git fetch && git checkout -B agent origin/main")
```

```python
# worker — the snapshot store is the client's own dependency, as in plain OpenAI usage. The plugin
# registers each provider's activities (lifecycle and sandbox operations) and run_tests.
AgentHarnessPlugin(
    tools=[run_tests],
    sandbox_clients=[
        SandboxClientProvider(
            "e2b",
            E2BSandboxClient(dependencies=Dependencies.with_values({"snapshots": S3SnapshotStore(...)})),
        ),
    ],
)
```

How the pieces fit the harness:
- **`Injected[SandboxSession]` parameter.** A tool asks for the sandbox with an ordinary `Injected[...]` annotation, so
  the existing machinery that strips injected parameters does all the hiding: the model schema, Code Mode's stubs and
  `tool_input` on the tool events never see it. The annotation also says plainly that the model doesn't supply it. The
  only difference from other injections is where the value comes from: the harness fills a `SandboxSession` injection
  itself, from the runner's sandbox, rather than from the caller's `run_tool(injections=...)` mapping.
  - Activity tools: the dispatcher calls `await runner._sandbox.ensure_running()` and puts the session state in
    `AgentToolContext.sandbox`. The parameter is left out of the activity's own arguments, because a live session can't
    be serialized. The activity body resumes the real session on the worker and passes it in.
  - Inline `@agent.tool_defn` tools get the workflow-side session (`runner.sandbox()`), where every call is an activity.
  - A tool that asks for a sandbox on an agent without one fails with a non-retryable `SandboxNotConfigured`. A tool may
    declare at most one `Injected[SandboxSession]` parameter.
- **Capability tools.** The same model as the OpenAI Agents Temporal plugin: the Temporal boundary is the session's
  operations (exec, read, write, PTY, persist/hydrate, lifecycle), each an activity, and the capabilities run in the
  workflow. So any capability works, OpenAI's or a developer's own, with nothing registered per tool.
  - `SandboxLifecycle` clones the configured capabilities and binds each to one `CapabilitySession`, a
    `BaseSandboxSession` that sends every call to the lifecycle's current `TemporalSandboxSession`, creating or
    resuming the sandbox first. So `runner.sandbox_tools()` can build the tools before the sandbox exists (asking
    creates nothing), and they keep working across idle shutdowns. Before creation it reports PTY support, so `Shell`
    offers `write_stdin`; on a backend without PTYs that tool then fails when called.
  - `runner.sandbox_tools()` calls each capability's `tools()` and wraps each `FunctionTool` (or `CustomTool`, whose one
    raw string becomes an `input` parameter) as an inline `@agent.tool_defn` tool. Its signature is built from the
    tool's JSON schema, so every model SDK and Code Mode's stubs see the name, description and parameters OpenAI
    tunes. A call ensures the sandbox is running, then calls `on_invoke_tool`. A non-Temporal exception (a malformed
    patch's `ValueError`) becomes a non-retryable `ApplicationError` for the caller instead of failing the workflow
    task. A `ToolOutputImage` result becomes a harness `SandboxImage`.
  - A `SandboxError` raised on the worker crosses the activity boundary with its fields in the `ApplicationError`'s
    details, and `CapabilitySession` raises it again as its own class, so a tool's own handling works (`write_stdin` to
    an exited process answers "PTY session not found" rather than failing). `runner.sandbox()` keeps raising
    `ActivityError`, which fails a handler cleanly where a plain exception in workflow code would fail the task.
  - Each capability's `process_manifest` applies to the manifest the sandbox is created with, and `SandboxConfig`
    checks `required_capability_types()`, both as OpenAI's runtime does. `sampling_params` and `process_context`
    shape an OpenAI Responses request, so they don't apply to harness tools.
  - Capability code runs in the workflow, so it must be deterministic and do its I/O through the session, the same
    contract as under the OpenAI plugin.
  - Harness approval policy replaces OpenAI's `needs_approval`.
- **The harness's `Shell` and `Filesystem`.** Drop-in subclasses of OpenAI's (same type, configuration and
  `configure_tools` hook) that swap OpenAI's own bundled tools, and only those, so a tool `configure_tools` put in
  their place is kept:
  - Shell output is cut inside the sandbox, so a large output never crosses to the worker, the model or history.
    `exec_command` wraps a non-TTY command in a POSIX `sh` script: the command runs in a subshell with its output
    in `/tmp/tool-output/<id>.log` (outside the workspace, so snapshots don't carry it); when it exits, the script
    strips ANSI codes and prints the whole log if it fits in `OUTPUT_BYTES` (12,000, or the model's
    `max_output_tokens` × 4) and deletes it, or else a header naming the log, the first third and the last half of
    the budget (errors and summaries come last). It exits with the command's code. The wrapper still runs on OpenAI's
    PTY, so a server keeps running as a session: the result then names the log for the model to `tail`, and a
    `write_stdin` check after it exits gets the summary. A `tty=true` command needs the terminal itself, so it and
    `write_stdin` keep OpenAI's worker-side `max_output_tokens` cap instead (default `OUTPUT_BYTES / 4`).
  - `apply_patch` is a freeform grammar tool in OpenAI. Here it is a function taking `patch: str`, with the one
    sentence of its description about the freeform format reworded. Two `*** Add File` mistakes whose intent is
    clear are repaired before OpenAI parses the patch, because models (Gemini especially) resend the same patch
    when told about them: a closing `+*** End Patch`, and a section with no `+` lines (an empty file such as an
    `__init__.py`), which gets one empty `+` line and so creates an empty file.
- **Images.** `view_image` (any tool returning a `ToolOutputImage`) gives a harness `SandboxImage` (mime type plus
  base64), whose `str()` is a short placeholder, so tool events and adapters without image support never carry the
  payload. The Gemini adapter's `function_result_value(result)` turns it into an image block for the
  `function_result` step. Other adapters are not yet built.
- **Code Mode / Monty.** The sandbox tools are ordinary inline harness tools, so a Monty script can loop over
  `exec_command(...)` with no extra work.
- **OpenAI `SandboxAgent`.** The runner passes its live session as `SandboxRunConfig(session=...)`. OpenAI's
  runtime treats an injected session as caller-owned (`runtime_session_manager.py`, `owns_session`) and never cleans it
  up, so one box persists across turns.
  - Today, without an injected session, every `Runner.run` owns its box and ends by tearing it down (`stop` to
    persist, `shutdown`, `client.delete`; `run.py:2225` → `runtime_session_manager.cleanup`). The next turn then pays
    for a rebuild and a restore from the snapshot.
  - The catch: a running injected session rejects capability or manifest changes between turns. Those must be fixed
    in `SandboxConfig`.

## Lifecycle (workflow-owned)

States: `absent → running ⇄ idle → closed`. `SandboxLifecycle` is a small runner-owned class whose state is the
`SandboxSessionState` plus a phase.

- **Create lazily** on first need: a sandbox tool call, an `Injected[SandboxSession]` tool, `runner.sandbox()`, or a `SandboxAgent`
  turn. Most turns never need a box, and creating one costs money and latency. Run `create` then `start`, and let OpenAI
  materialize the manifest or restore the snapshot.
- **Concurrency.** Parallel tool calls in one turn go through a workflow `asyncio.Lock` around `ensure_running()`.
- **Idle policy** (ported from sandbox-orchestration-harness, which runs an idle timer and does nothing per turn): the
  run loop waits `wait_condition(has_pending_turns or closed, timeout=idle.after)`. On timeout it applies `idle.action`:
  - `"keep"`: do nothing.
  - `"persist"`: `stop()` writes the workspace to the snapshot and the box keeps running.
  - `"persist_and_shutdown"`: `stop()` then `shutdown()` to release compute. The next `ensure_running()` calls
    `client.resume(state)`, which rebuilds the box and restores the workspace from the snapshot. This is the
    **provider-agnostic form of the Go repo's suspend-via-snapshot fallback**, and it already works on every OpenAI
    backend.
  - `"pause"`: native pause where the backend has `pause_on_exit` (E2B, Daytona, Runloop, Blaxel); otherwise a config
    error.

  Provider-side idle settings (E2B `on_timeout="pause"` with `auto_resume`, Daytona `auto_stop_interval`) stay
  available as a backstop.
- **Box lost mid-run** (not yet built). If an operation fails with a transport or not-found error, the lifecycle marks
  the box lost. The next `ensure_running()` calls `client.resume(state)`, which rebuilds and restores from the snapshot,
  and the tool call is retried once. Without a durable snapshot, the workspace is rebuilt from the manifest alone, and
  the agent should be told. Today the worker-side `client.resume(state)` on a session-cache miss is the only recovery.
- **Close.** In `run()`'s outer `finally`: `stop` (a final persist, only when `keep_snapshot`), `shutdown`,
  `client.delete`, then snapshot retention. A failure doesn't skip the later steps. Failures are logged rather than
  raised, so a backend that refuses to clean up never fails the agent's own completion; lifecycle activities default to
  five attempts for the same reason. This matches OpenAI's own
  cleanup. A hard `TERMINATE` runs no workflow code, so set provider timeouts (E2B `timeout`, Daytona auto-stop/delete).
- **Snapshot retention** (OpenAI has no delete API): the lifecycle records the snapshots it used. On close it
  deletes them, unless `SandboxConfig.keep_snapshot=True` (for example, to resume a later conversation from one). A
  `LocalSnapshot` file is unlinked. A `RemoteSnapshot` is deleted through an optional `delete(snapshot_id)` on the store
  that the client's dependencies bind to its key.
- **Lifecycle activities return the state.** `start`, `stop` and `shutdown` change the session state on the worker (`stop`
  records the snapshot fingerprint a later resume checks), so they return it and the workflow's copy stays current.
  That also fixes a stale-state bug in the OpenAI integration's adapter.

## Durability rules (the Temporal-specific part)

1. **Snapshots must live in shared storage.** OpenAI's default is a `LocalSnapshotSpec` under a directory on the worker,
   which another worker can't read. `SandboxConfig` validation rejects local snapshots unless `dev_mode=True`. Use
   `RemoteSnapshotSpec` backed by a worker dependency (S3/GCS), or the provider's native `workspace_persistence`
   (E2B `"snapshot"`, Modal `"snapshot_filesystem"`, Runloop `snapshot_disk`).
2. **Workspace bytes stay off the normal path.** The lifecycle persists with `session.stop()` on the worker, so archives
   go straight to the snapshot store and only the small state enters history. The existing
   `persist_workspace`/`hydrate_workspace` activities return the archive to the workflow. External payload storage
   (enabled by default in the harness) makes that safe for occasional use, but nothing in this design calls them
   routinely.
3. **PTY affinity.** Backends keep PTY process registries in worker memory (E2B `_pty_processes`, Daytona
   `_pty_sessions`). A `write_stdin` that lands on another worker raises `PtySessionNotFoundError`. The fix pins the
   whole session rather than individual PTY calls, which needs no PTY-specific plumbing and also keeps cache hits on one
   worker:
   - `SandboxClientProvider(..., affinity_task_queue=...)` names a queue only that worker process polls, with the same
     activities registered (the standard worker-specific task-queue pattern).
   - `create`/`resume` return that queue, and every later activity for the session, sandbox tools included, is routed
     there with a `schedule_to_start_timeout` (`SandboxConfig.affinity_timeout`, one minute by default).
   - If that timeout fires, the worker is presumed gone: the session unpins, the call fails with a non-retryable
     `SandboxWorkerLost` ("processes started earlier are gone; start them again"), and the next call goes to the
     shared queue, where any worker resumes the session from its state.
   - Without an affinity queue, behaviour is as before: fine for one worker, and a `write_stdin` across workers fails.
4. **Bounded session cache.** `SandboxClientProvider._sessions` is an LRU (`max_cached_sessions`, 128 by default)
   evicted on `delete`. `shutdown` keeps the entry, because `client.delete` follows and needs that same object (resuming
   a shut-down box only to delete it could rebuild it).
5. **Retries.** `SandboxError(retryable=False)` becomes a non-retryable `ApplicationError` (already implemented).
   Activity retries re-run `exec`, which is the same at-least-once contract every activity tool already has. Tools
   should say if they aren't idempotent.
6. **Secrets.** Provider API keys only exist where the worker constructs the client. Not yet verified: the adapter
   serializes `SandboxSessionState` with plain pydantic, not the client's `serialize_session_state` (which redacts mount
   credentials), so a manifest with credentialed mounts may put them in history. Check this before enabling such
   mounts.
7. **Determinism.** OpenAI sandbox types are imported pass-through in workflow modules. Capability and tool code runs
   in the workflow, but every sandbox operation it makes is an activity, so no `agents.sandbox` I/O runs there. A
   capability that does other I/O or anything nondeterministic breaks replay, as it would under the OpenAI plugin.

## Relation to existing work

- `ai_sdks/openai_agents/sandbox/`: `SandboxClientProvider`, `TemporalSandboxClient`, `TemporalSandboxSession` and the
  activity models moved to `harness/sandbox/` as the shared base. The public names are re-exported at the old package
  path. The OpenAI Agents integration is now one consumer: its plugin's `sandbox_clients=` registers the same
  activities as `AgentHarnessPlugin(sandbox_clients=...)`, and both run capabilities in the workflow on them.
  Passing the same providers to both is fine: activities are merged by name.
- `sandbox-tools` branch (remote-box): a different model, where *your Python runs in the box* versus *your tools act
  on a box*. This design takes over its plumbing ideas: by-name worker registration, `SandboxNotConfigured`, the
  run-loop lifecycle hooks, core never importing the optional dependency, and the `imports_passed_through` rule. It also
  takes over its coding-agent example (Gemini with Shell + Filesystem tools). Retire remote-box, or keep it as a
  separate opt-in if "run Python remotely" still has a use case. That's an open question.

## Phasing

1. **Move and harden the adapter** (done): move it under `harness/sandbox/`, then fix PTY affinity (rule 3) and cache
   eviction (rule 4). Tests run on OpenAI's in-core `unix_local` backend rather than a fake client: still no network,
   and it exercises a real backend.
2. **Runner lifecycle** (done): `SandboxConfig`, `SandboxLifecycle` (lazy create, idle policy, close, snapshot
   retention), `runner.sandbox()`, and the snapshot-storage validation.
3. **Tool surfaces** (done): the `Injected[SandboxSession]` tool parameter, any capability's tools in the workflow
   with `runner.sandbox_tools()`/`sandbox_instructions()`, and `view_image` mapping for the Gemini adapter first.
4. **OpenAI `SandboxAgent` on the runner-owned session** (done: `await runner.sandbox_run_config()`). Not yet built:
   porting the sandboxed coding-agent example.
5. **Later:** lost-box recovery tuning, sharing a sandbox with subagents (likely a child-workflow owner), capabilities
   whose `instructions()` await the session (`sandbox_instructions()` is synchronous), and streaming exec output to
   the UI.

## Open questions

- Should one agent be able to hold more than one sandbox (several `SandboxConfig`s keyed by name)? v1 says one.
- Subagents: inherit the parent's sandbox (needs a shared owner), or always get their own?
- Should remote-box (`sandbox-tools`) be retired, or kept for the "run my Python remotely" case?
- What's the version policy for a pre-1.0 dependency with frequent minor releases: a lower bound plus CI against the
  latest, or an upper bound per harness release?
