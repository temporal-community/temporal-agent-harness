# Build with a coding agent

The Python and TypeScript SDKs each bundle a language-specific skill, focused API references,
and an OpenAI starter. These resources travel with the package version so an agent can work in
your application without cloning or reading the harness implementation.

> The commands on this page ship with the next package releases. They are not present in the
> previously published Python 0.8.0 and npm 0.2.0 packages.

## Install and discover the skill

Run from your application root after installing the SDK. Each coding agent discovers project
skills in its own directory, so install for each tool you use:

| Tool | Skill directory | Command | Invoke it with |
| --- | --- | --- | --- |
| Codex | `.agents/skills` (default) | `skills install` | `$temporal-harness-python` / `$temporal-harness-typescript` |
| Claude Code | `.claude/skills` | `skills install --for claude` | `/temporal-harness-python` / `/temporal-harness-typescript` |
| Another tool | its own | `skills install --dest <directory>` | per that tool |

```bash
# Python
uv run temporal-agent-harness skills path
uv run temporal-agent-harness skills install
uv run temporal-agent-harness skills install --for claude

# TypeScript: the command comes from @temporalio/agent-harness-worker
npx agent-harness skills path
npx agent-harness skills install
npx agent-harness skills install --for claude
```

`skills path` prints the bundled directory. `skills install` copies it into
`<skill directory>/temporal-harness-python` or `<skill directory>/temporal-harness-typescript`
and reports whether anything changed. `--for` is repeatable. Reload your agent session if it was
already running. Installing an SDK does not automatically activate a skill or write into your project.

Ask the agent to use the language's skill. Its short `SKILL.md` routes to authoring, API,
advanced-feature, and verification references and to the bundled starter, which is installed
beside it as the complete worked example. It tells the agent to preserve your existing provider
and framework, to inspect public types and generated schemas first, and to verify with the
model-free `ping` handler before spending model tokens.

## Start an application

For a new project, follow the [Python quickstart](../python/README.md#quickstart) or
[TypeScript quickstart](../typescript/packages/core/README.md#quickstart). They bootstrap the CLI
with `uvx` or `npx`, then create the project, install its skill, and start the services.

```bash
# Choose one, from a project where the SDK is installed:
uv run temporal-agent-harness init ./my-agent
npx agent-harness init ./my-agent
```

Follow the generated README. Each starter contains an agent, worker, registry, dependency
manifest named after the directory, environment template, and schema-generation command. `init`
pins the installed harness version and refuses to overwrite existing files. It does not install
dependencies, start services, or configure credentials for you.

The `ask` handler demonstrates a streaming model call and a safe weather tool with sample data.
The `ping` handler echoes a message without a model request, and the worker starts without a
provider key, so an agent can verify connectivity before spending model tokens. Both starters
use the same three-process architecture:

1. Temporal service.
2. `temporal-agent-harness dev agents.toml`: the Python session-manager worker and the API and
   console server in one process. `session-manager` and `serve` remain available separately.
3. Your Python or TypeScript agent worker.

For TypeScript applications, the starter's `npm run services` runs `dev` through `uvx`; no
Python project is needed. There is currently no Node.js implementation of the shared server.
The TypeScript CLI is one command: `agent-harness init|schema|skills|doctor`, where `schema` is
the existing `agent-harness-schema` binary.

## Diagnose and verify

```bash
uv run temporal-agent-harness doctor --registry agents.toml --api-url http://localhost:8000
npx agent-harness doctor --schema agent.schema.json --api-url http://localhost:8000
```

Both commands report versions, API reachability, and the API's agent-worker poller status.
The Python command also validates the TOML registry and its referenced schema names, connects
directly to Temporal, and checks session-manager and agent queues. The TypeScript command
checks the supplied agent schema's basic shape. It uses the API for worker availability.
Use Python's doctor when you need direct Temporal or TOML diagnostics.

`--json` produces a report for automation; a failed check exits 1. `--timeout` is in seconds
per service probe. These commands only read configuration and service status. They never
create sessions, send messages, or reset Temporal. A reported poller does not prove the worker
registered the correct workflow type or that the provider credentials work.

After startup, verify `ping`, then a streamed `ask` reply and its tool trace. Reload the console
to test reconnect. When adding approvals or callbacks, test their waiting and resumed states.
Use the [API guide](api/README.md) for session creation, message acceptance, stream cursors,
and generated client types. `schema --openapi` exports the HTTP contract without running a server.

## Update without losing project changes

Upgrade the SDK and rerun `skills install`. An ownership manifest records the SDK version and
file hashes. The command updates unchanged bundled files, removes obsolete unchanged bundled
files, and leaves unrelated files alone. It checks all conflicts before writing: if you edited
a bundled file, it stops and preserves your work. Reconcile your edits or install the new copy
under another `--dest` to compare. Symlink destinations are refused.

Starters are copied once. Updating a skill never rewrites your application code.
Commit generated schemas and dependency lockfiles, and regenerate client types after changing
handlers, state, or init data. Use a fresh session for changed workflow behavior during local
development; existing histories need normal Temporal replay/versioning discipline.

## Maintaining these resources

The canonical skills live in the packages:

- [Python](../python/temporal_agent_harness/skills/temporal-harness-python/SKILL.md)
- [TypeScript](../typescript/packages/core/skills/temporal-harness-typescript/SKILL.md)

Repository `.agents/skills/` links point to these canonical directories for project discovery,
beside `temporal-harness-repo`, a repository-only skill for contributors working in the checkout
(Bazel targets, `just` recipes, generated registries). It is not packaged.
The Python wheel/sdist and core npm tarball include the complete resource trees. The worker
npm package provides `agent-harness`. The shared API reference is authored in
[docs/api/README.md](api/README.md); run `python3 scripts/sync-agent-resources.py` after editing it.
CI checks the copies with `--check`. Package installation tests generate and compile starters,
install skills, export schemas, and run a model-free handler with the installed artifacts.
The TypeScript starter pins the Python package that provides the shared services in its
`package.json` `services` script; the release script updates that pin with the other
documentation pins.
