# Development

Contributor-facing notes for working in this repository from a checkout: the layout,
the root `justfile`, UI development, and building/packaging. (End-user installation and
usage lives in the top-level [`README.md`](../../README.md).)

## Repository layout

```
temporal_agent_harness/
├── harness/      # the core harness: agent contract, turn runner, tools, Code Mode,
│                 #   the agent/subagent protocol, human-in-the-loop approvals
├── ai_sdks/      # AI SDK integrations (Gemini today) — durable activity wrappers
├── web/          # packaged session-manager workflow + FastAPI app factory
└── utils/        # general Temporal utilities (e.g. large-payload offload)

examples/
├── monty/          # a travel-booking agent example with packaged web/UI wiring
└── callback_tools/
    └── wiki_agent/ # a wiki-organizing agent built on callback tools, with a thin terminal client

ui/               # shared Svelte frontend for the Monty example

tests/            # mirrors the package layout
```

## Root Justfile

The repo root has a `justfile` for the common development workflow. Run
`just --list` from the repo root to see the available recipes.

Build and package recipes run directly from the root:

```bash
just app-install   # install Svelte dependencies
just app-build     # build ui/ into temporal_agent_harness/ui/dist
just app-check     # Svelte checks
just package       # UI build + UI checks + pytest + uv build
```

Local stack recipes delegate into `examples/monty`:

```bash
just temporal          # local Temporal dev server
just session-manager   # packaged session-manager worker
just server            # serves the COMMITTED UI build + FastAPI API on http://localhost:8000
just dev-server        # rebuild the UI first, then serve (use after editing ui/)
just monty-worker      # Monty agent worker
just ui-dev            # Vite hot reload on http://127.0.0.1:5173
```

`just server` deliberately does **not** rebuild the UI: it serves the committed
`temporal_agent_harness/ui/dist`, so a downloaded release archive runs with no Node/pnpm
installed. That makes the committed build load-bearing — after changing anything under `ui/`,
run `just app-build` (or `just dev-server`) and commit the result.

All example recipes read one shared env file, `.env.local` at the repo root: the root
justfile loads it directly, and `examples/*/justfile` read the same root file (via
`dotenv-path`). Keep your Temporal profile + `GEMINI_API_KEY` there.

## UI Development

The source Svelte app lives in [`ui/`](../../ui). The package ships the compiled
output in [`temporal_agent_harness/ui/dist`](../../temporal_agent_harness/ui/dist), so
any UI change needs a rebuild before packaging or before `just server` serves it:

```bash
just app-install   # one-time install of Svelte dependencies
just app-build     # build ui/ into temporal_agent_harness/ui/dist
just app-check     # svelte-check + local Svelte 5 syntax guard
```

For hot reload, keep the FastAPI API server running on port 8000 and start Vite
in another terminal:

```bash
# terminal 1
just server

# terminal 2
just ui-dev
```

The production build uses relative asset and API URLs, so the UI can be served
from `/` or under a path prefix as long as the UI and API are mounted together.

## Build And Package

Use the package recipe from the repo root:

```bash
just app-install   # one-time setup if ui/node_modules is absent
just package
```

The resulting artifacts are written to `dist/`:

```text
dist/temporal_agent_harness-0.3.0.tar.gz
dist/temporal_agent_harness-0.3.0-py3-none-any.whl
```

The wheel and sdist include:

- the core harness package
- `temporal_agent_harness.web` with the FastAPI app factory and session-manager worker helper
- `temporal_agent_harness.ui/dist` with the built Svelte UI assets
- the `ui` extra, which pulls in `fastapi[standard]`

Before publishing or handing off artifacts, run:

```bash
just package
```

`just package` runs the Svelte production build, Svelte checks, the local
Svelte 5 syntax guard, the Python test suite, and `uv build`. The primary
recipe lives in the repo-root `justfile`; the same recipe is also available
from `examples/monty/justfile` for convenience.

## Bazel

Bazel (9.x, via [bazelisk](https://github.com/bazelbuild/bazelisk); `.bazelversion` pins the
release) builds and tests the Python package, the examples, and the TypeScript generated from
them. It sits beside the uv/just workflow above; `pyproject.toml` and `uv.lock` stay the source
of truth for Python dependencies. Nexus (`nexus/`, nexgen) is not built by Bazel.

```bash
bazel test //...                          # every test on Python 3.13
bazel test //... --config=py311           # the same on 3.11 (also py312, py314)
bazel test //tests/state:test_paths --test_filter=escaped  # one module, pytest -k
just gazelle                              # after adding, moving, or changing imports of .py files,
                                          # or changing uv.lock (gazelle_python.yaml, then BUILD files)
just buildifier                           # format and lint-fix every Bazel file
bazel run //:venv_link                    # link ./.venv-bazel for an IDE interpreter
bazel run //examples/monty:worker         # an example's agent worker
bazel run //examples/tictactoe:agents_update     # rewrite an example's agents.toml and schemas
bazel run //examples/tictactoe:client_sdk_update   # rewrite a committed client SDK
bazel run //packages/client:protocol_update        # rewrite packages/client/src/protocol.ts
```

- **Targets are generated.** Gazelle writes one `py_library` (or test, or binary) per `.py` file,
  with `deps` from that file's imports, so `bazel test` reruns only the tests a change can reach.
  Directives live in the root `BUILD.bazel`. Gazelle cannot see a module loaded by name (e.g.
  `load_agent_class("examples.x.workflow:Agent")`) or `from . import x`; those deps are added by
  hand with `# keep`, which Gazelle preserves. CI (`bazel-files` in `tests.yml`) fails when
  `just gazelle` would change anything or buildifier finds a file unformatted or with a lint
  warning, so run `just gazelle` and `just buildifier` before pushing.
- **Python** uses [aspect rules_py](https://github.com/aspect-build/rules_py) 2.x: its uv
  extension reads `uv.lock` into the `@pypi` hub (the `dev` dependency group), and it provisions
  the interpreters. The interpreter is a build setting, 3.13 by default; `--config=py3XX` selects
  another for a whole invocation.
- **Tests** are `harness_pytest_test` (`bazel/pytest.bzl`): pytest with the repo's
  `pyproject.toml` config, the Temporal servers pinned with checksums in `MODULE.bazel` (handed to
  `WorkflowEnvironment` by `tests/conftest.py`, so no test downloads one), and Code Mode's `monty`
  binary. A few tests are deselected under Bazel; each target says why.
- **Binaries** are `harness_module_binary` (`bazel/binary.bzl`), which runs a file as
  `python -m <module>` so package-relative imports work. Under `bazel run` it runs from the
  workspace root, so repo-relative paths (`.env.local`'s `TEMPORAL_CONFIG_FILE`, `agents.toml`)
  resolve as they do for the justfiles' recipes.
- **Running the examples.** The justfiles' run recipes call `bazel run` (loading `.env.local`
  first). The session manager and `serve` are `//temporal_agent_harness/web:cli`, which imports
  no agent code. `//examples:dev`, `:workers`, and `:server` are `harness_dev` targets
  (`bazel/dev.bzl`): `bazel run //examples:dev -- monty wiki_agent` runs the session manager, the
  server over those examples' registries, and their workers, every example when none are named.
  `bazel/dev_runner.py` starts them with the environment `bazel run` would give each, prefixes
  every output line with the program's name, and forwards Ctrl-C to all of them. The root
  `server`, `workers`, and `dev` recipes run these targets. Bazel holds its lock only while
  building, so separate `bazel run`s can also run side by side. The
  examples also run with uv alone (README, "Without Bazel"), since every generated file they read
  is committed.
- **Agent registries are generated.** Each example declares its agents with `harness_agent`
  (`bazel/agents.bzl`): the class and the fields of its `agents.toml` entry. `harness_agent`
  writes the agent's schema document with the CLI (`schema module:Class`), and
  `harness_agent_registry` writes `agents.toml` with each entry's `workflow_type` read from that
  schema and `schema` pointing at it. Both are committed beside the BUILD file
  (`bazel run :agents_update`) and checked by `bazel test`, so `serve` reads every agent's start
  interface from them without importing agent code, under Bazel or uv alike. `nexus_hello`,
  which Bazel does not build, keeps a hand-written registry and schema.
- **Generated TypeScript** (`examples/*/client_sdk/`, `packages/client/src/protocol.ts`) is built
  from those schema documents (`harness_schema` for the protocol's) by the `harness_types` rule
  (`bazel/codegen.bzl`) through `packages/codegen` (rules_js). An example declares its clients
  with `harness_client_sdk`.
- **Committed generated files** all go through `committed_files` (`bazel/committed_files.bzl`),
  which `harness_agent_registry` and `harness_client_sdk` use too: `bazel run :<name>_update`
  writes them, and `<name>_<file>_up_to_date_test` fails while one differs from what Bazel
  generates.
  `packages/codegen/pnpm-lock.yaml` is derived from its `package-lock.json` by rules_js
  (`pnpm import`) whenever the latter changes.
