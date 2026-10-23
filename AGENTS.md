# Working in this repository

Temporal Agent Harness is a Python SDK (Typescript support coming soon) for building durable agents, with a development+observability web console and
TypeScript clients for building products over the agents. This file covers contributing; [README.md](README.md) covers usage.
Run commands below from the repository root unless stated otherwise.

## Find the right code

- `python/temporal_agent_harness/harness/`: agent runtime, tools, and orchestration.
- `python/temporal_agent_harness/ai_sdks/`: AI SDK integrations.
- `python/temporal_agent_harness/web/`: CLI, session manager, and FastAPI service.
- `python/examples/`: runnable agents; examples are not shipped in the Python package.
- `python/tests/`: Python tests, organized by subsystem.
- `typescript/apps/ui/`: Svelte 5 console; `typescript/packages/`: clients and codegen.
- `contract/` and `conformance/`: shared schemas and cross-SDK behavior fixtures.
- `bazel/`: build rules; `nexus/`: connectors and Nexus tooling, outside the Bazel build.

## Run locally

Use `just` and Bazelisk installed as `bazel`; local Temporal also needs the `temporal` CLI.
Bazel provisions the build toolchains and dependencies. See `just --list` for recipes.

1. If `.env.local` is absent, copy `.env.example` to it and configure the selected
   examples' credentials. Preserve existing settings; never commit credentials.
2. Run `just temporal` in one terminal, unless the configured Temporal server is already up.
3. Run `just dev monty` in another, then open <http://localhost:8000>.
   `just dev` runs all examples; some need additional keys or callback clients (see README).

`just dev` starts the session-manager worker, API/UI server, and agent workers;
it does **not** start Temporal. The run recipes load the root `.env.local`.
For UI hot reload, keep the API on port 8000 and run `just ui-dev` separately.
`just server` serves the UI built by Bazel; restart it to pick up frontend changes.

## Make changes

- Follow neighboring code and tests; keep changes focused on the requested behavior.
- Use absolute Python imports rooted at `temporal_agent_harness`, `examples`, or `tests`.
- Python dependencies live in `python/pyproject.toml` and `python/uv.lock`;
  JavaScript dependencies use the `typescript/` pnpm workspace and its single lockfile.
  Use Bazel's pinned pnpm: `bazel run //typescript:pnpm -- <args>`.
- Python BUILD targets are generated. After adding, removing, moving, or changing imports
  in Python files, or changing `uv.lock`, run `just gazelle` and review the generated diff.
  Preserve Gazelle directives and `# keep` dependencies for dynamically loaded modules.
- TypeScript BUILD files are maintained by hand: wire new sources and tests into them.
  Run `scripts/check-ts-test-targets.sh` when adding or moving TypeScript tests.
- Run `just buildifier` after changing Bazel files. New Python package entry points must
  be reachable from `python/temporal_agent_harness/BUILD.bazel`; verify with
  `scripts/check-python-package-entry-points.sh`.
- Edit frontend sources, not compiled assets. UI build output is not committed.

## Regenerate derived files

Change the source declarations, then run the relevant generator and include its output.
Do not fix an out-of-date generated-file test by editing the generated file directly.

| Changed source | Regeneration command |
| --- | --- |
| Example agent declaration or init schema | `bazel run //python/examples/<example>:agents_update` |
| Example client schema (where a client SDK is declared) | `bazel run //python/examples/<example>:client_sdk_update` |
| Event protocol | `bazel run //contract:protocol_update` then `bazel run //typescript/packages/client/src:protocol_codegen_update` |
| Registry format | `bazel run //contract:registry_update` |
| Intentional conformance behavior | `bazel run //python/tests/conformance:goldens_update` |

For example registries, edit the `harness_agent` declarations and agent classes.
The non-Bazel `nexus_hello` example keeps a handwritten registry and schema.
Read [contract/README.md](contract/README.md) and [conformance/README.md](conformance/README.md)
before changing protocol schemas or golden fixtures; review their diffs for behavior changes.

## Validate and report

Start with tests for the affected behavior; find target names in the nearby `BUILD.bazel`.

- One Python test module: `bazel test //python/tests/state:test_paths` (example target).
  Add `--test_filter=<pattern>` to select tests within a module.
- Console changes: `just app-check` runs the UI type checks and tests.
- TypeScript client changes: `bazel test //typescript/packages/client/...`.
- Broad changes: `bazel test //...`; packaging changes: `just package`.
- CI tests Python 3.11–3.14; use `--config=py311` (or `py312`, `py313`, `py314`)
  when checking version-specific behavior. Nexus and other excluded tests use `uv`;
  consult [.github/workflows/tests.yml](.github/workflows/tests.yml) for the exact commands.

Add regression coverage for behavior changes. For documentation-only edits, check links,
commands, and `git diff --check`; no application test run is needed.
Before handing off, review the diff and report what changed, what checks ran, and any blockers.
More build and packaging details: [docs/internal/development.md](docs/internal/development.md).
