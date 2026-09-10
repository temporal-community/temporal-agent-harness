# Codex Hello

A small repository guide with **Codex CLI as the inner agent engine**. Ask about a
codebase and watch real file-inspection commands, progress, and the answer in the
harness UI. The workflow is a normal `AgentWorkflowRunner` with one `ask` handler;
`CodexPlugin` provides the worker activity that runs the CLI and publishes its events.

## Run in the shared harness UI

Install Codex CLI and sign in once with `codex login` on the worker machine. This
example uses that saved login; an `OPENAI_API_KEY` is not required. You also need
Python/uv, just, and the Temporal CLI, as for the other examples. Configure the root
`.env.local` as described in the [shared example setup](../../README.md#run-the-examples);
the other agents use the API keys listed there.

From the repository root, check the local setup:

```sh
just codex-check
```

Then run these commands in separate terminals, all from the repository root:

```sh
just temporal          # skip if your Temporal server is already running
just session-manager   # shared session-manager worker
just server            # all example agents at http://localhost:8000
just workers           # all seven agent workers, including Codex Hello
```

If the shared session manager already exists with an older registry, run
`just reset-manager` before starting `just server` so Codex Hello appears.

Open <http://localhost:8000>, select **Codex Hello**, and ask:

> Inspect this repository with read-only commands. What are its main entry points,
> and where are the tests? Cite the files you inspected.

The turn shows Codex commands starting and finishing, their output and exit status,
and the agent's progress and answer. Expand the tool entries to inspect the actions.
Codex emits messages and item updates as they become available; the CLI's JSON event
stream does not promise a token-by-token text stream.

To run only the Codex worker, replace `just workers` with `just worker-codex-hello`.
It reads this checkout by default. To inspect another project, use:

```sh
just worker-codex-hello --workspace /path/to/project
```

Use a single workspace per task queue: stop the old worker before replacing it, or
choose a different `--task-queue` and update `agents.toml` to match.

## Run standalone

Without just, start these commands in separate terminals from the repository root:

```sh
# 1. Local Temporal server; skip if already running.
temporal server start-dev

# 2. Shared session manager.
env TEMPORAL_CONFIG_FILE=temporal.local.toml TEMPORAL_PROFILE=default \
  uv run --group examples python -m examples.session_manager_worker

# 3. Shared harness API and packaged UI.
env TEMPORAL_CONFIG_FILE=temporal.local.toml TEMPORAL_PROFILE=default \
  uv run --group examples python -m examples.app examples/codex_hello/agents.toml \
  --host 127.0.0.1 --port 8000 --manager-id codex-hello-sessions

# 4. Codex worker, reading this harness checkout by default.
uv run --group examples python -m examples.codex_hello.worker
```

The server command uses `--manager-id codex-hello-sessions`
to keep this example's registry separate from an existing shared session manager.
If another UI is already on port 8000, select a different `--port` and open that URL.
The worker accepts `--workspace /path/to/project` and `--check` just as the root recipes do.

From `examples/codex_hello`, the scoped justfile also provides `just temporal`,
`just session-manager`, `just server`, `just worker`, and `just check`.

## Run from the terminal

The server and session manager are optional for CLI use. With Temporal and the Codex
worker running, send a question and print the same harness events as JSON lines:

```sh
just codex-client --id codex-demo \
  "Inspect the repository and explain how a user message reaches the agent."
```

Reuse `--id codex-demo` for a follow-up, or replay and follow its existing activity:

```sh
just codex-client --id codex-demo \
  "Which tests cover that path? Inspect them and cite the files."
just codex-client --id codex-demo --watch
```

Without just, replace `just codex-client` with
`uv run --group examples python -m examples.codex_hello.client`.

The client prints a link to the session in the harness UI. Closing the client leaves
the durable workflow running; `--watch` can reattach. The workflow retains the last
six conversation messages, capped at 8,000 characters each, and starts a fresh Codex
run for every question.

## Integration and permissions

The essential wiring is:

```python
# Worker: plugin registers the CLI activity and configures the Temporal client.
plugin = CodexPlugin(CodexConfig(workspace="/path/to/project", sandbox="read-only"))
client = await Client.connect("localhost:7233", plugins=[plugin])

# Workflow: pass the runner so native Codex events route into the active turn.
result = await run_codex(prompt, runner=self._runner)
return TextReply(text=result.text)
```

The example fixes the Codex sandbox to `read-only` and does not expose filesystem
permissions through chat. **Codex's native tools run under worker configuration;
the harness's per-tool approval policy does not intercept those tool calls.** Their
tool cards are execution reports, not harness approval requests. To build a coding
agent, explicitly configure the plugin's workspace permissions in your worker.

The default timeout is 600 seconds per question; override it with
`--timeout-seconds`. `--model` optionally selects a Codex model; leaving it unset
uses the CLI's default. Worker diagnostics are kept in `.codex-runs/` by default
and can be moved with `--state-dir`.

See the [Codex non-interactive mode documentation](https://developers.openai.com/codex/noninteractive)
for `codex exec` and its JSON event stream.
