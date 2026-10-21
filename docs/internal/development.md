# Development

Contributor-facing notes for working in this repository from a checkout: the layout,
the root `justfile`, UI development, and building/packaging. (End-user installation and
usage lives in the top-level [`README.md`](../../README.md).)

## Repository layout

```
python/               # the Python SDK as a uv project (pyproject.toml, uv.lock)
├── temporal_agent_harness/
│   ├── harness/      # the core harness: agent contract, turn runner, tools, Code Mode,
│   │                 #   the agent/subagent protocol, human-in-the-loop approvals
│   ├── ai_sdks/      # AI SDK integrations (Gemini today) — durable activity wrappers
│   ├── web/          # packaged session-manager workflow + FastAPI app factory
│   └── utils/        # general Temporal utilities (e.g. large-payload offload)
├── examples/
│   ├── monty/          # a travel-booking agent example with packaged web/UI wiring
│   └── callback_tools/
│       └── wiki_agent/ # a wiki-organizing agent on callback tools, with a thin terminal client
└── tests/            # mirrors the package layout

typescript/           # one pnpm workspace (pnpm-workspace.yaml, one pnpm-lock.yaml)
├── packages/         # client, react, svelte (published to npm), codegen
└── apps/
    ├── ui/           # the console: the Svelte frontend the Python package serves
    └── chat-server/  # Chat SDK front end for Slack/Discord
```

## Root Justfile

The repo root has a `justfile` for the common development workflow. Run
`just --list` from the repo root to see the available recipes.

Build and package recipes run directly from the root:

```bash
just app-install   # install the typescript/ workspace, for `ui-dev`
just app-check     # Svelte checks
just package       # Python and UI tests, then the wheel and sdist (//python:dists)
```

Local stack recipes delegate into `python/examples/monty`:

```bash
just temporal          # local Temporal dev server
just session-manager   # packaged session-manager worker
just server            # the UI + FastAPI API on http://localhost:8000
just monty-worker      # Monty agent worker
just ui-dev            # Vite hot reload on http://127.0.0.1:5173
```

`just server` serves the UI as Bazel builds it from `typescript/apps/ui/`
(`//python/temporal_agent_harness/ui:dist`), so a change there shows up on the next run. No
build of the UI is committed.

All example recipes read one shared env file, `.env.local` at the repo root: the root
justfile loads it directly, and `python/examples/*/justfile` read the same root file (via
`dotenv-path`). Keep your Temporal profile + `GEMINI_API_KEY` there.

## UI Development

The source Svelte app lives in [`typescript/apps/ui/`](../../typescript/apps/ui), a package of
the pnpm workspace in `typescript/`. Bazel builds it (`//typescript/apps/ui:dist`) into the
Python package's `temporal_agent_harness/ui/dist`, which `just server` serves and the wheel and
sdist ship:

```bash
just app-install   # install the typescript/ workspace, for `ui-dev`
just app-check     # svelte-check + local Svelte 5 syntax guard
```

Every pnpm command in the justfiles runs through Bazel: `bazel run //typescript:pnpm -- --dir
<package> <args>` is the pnpm that `typescript/package.json`'s `packageManager` pins, on Bazel's
Node, and the scripts it runs get that Node too. Nothing JavaScript needs installing on the
machine. Running `pnpm` yourself in `typescript/` works as well with pnpm 12; an older pnpm may
fail to switch itself to the pinned version, so use the Bazel target there.

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

Bazel builds the wheel and sdist PyPI publishes as `//python:dists`, a directory holding both
(`bazel/python_dists.bzl`). It runs setuptools' build backend on a staged copy of the project, so
`python/pyproject.toml` decides what ships and with what metadata, as it would for `uv build`;
the UI comes from Bazel's build of `typescript/apps/ui/`. From the repo root:

```bash
just package       # bazel test //python/... //typescript/apps/ui/..., then bazel build //python:dists
```

The wheel and sdist include:

- the core harness package
- `temporal_agent_harness.web` with the FastAPI app factory and session-manager worker helper
- `temporal_agent_harness.ui/dist` with the built Svelte UI assets
- the `ui` extra, which pulls in `fastapi[standard]`

`//python/tests:test_packaged_distributions` checks those archives, and
[Publish to PyPI](../../.github/workflows/publish.yml) uploads them after `bazel test //python/...`.
What ships is what `//python/temporal_agent_harness` reaches under
`python/temporal_agent_harness/`: its deps are the package's entry points (the modules nothing
else in the package imports), and their sources and data (the UI) make up the archives. A new
entry point, such as an SDK integration nothing imports, must be added to those deps;
`scripts/check-python-package-entry-points.sh` fails in CI until it is. Modules Bazel does not
build, like `nexus_agent_adapter/`, are not published. A package installed from a git checkout
(`pip install git+https://...`) has no UI, and `serve` says so.

## Bazel

Bazel (9.x, via [bazelisk](https://github.com/bazelbuild/bazelisk); `.bazelversion` pins the
release) builds and tests the Python package, the examples, and the TypeScript generated from
them. It sits beside the uv/just workflow above; `python/pyproject.toml` and `python/uv.lock` stay
the source of truth for Python dependencies. Nexus (`nexus/`, nexgen) is not built by Bazel.

```bash
bazel test //...                          # every test on Python 3.13
bazel test //... --config=py311           # the same on 3.11 (also py312, py314)
bazel test //python/tests/state:test_paths --test_filter=escaped  # one module, pytest -k
just gazelle                              # after adding, moving, or changing imports of .py files,
                                          # or changing uv.lock (gazelle_python.yaml, then BUILD files)
just buildifier                           # format and lint-fix every Bazel file
bazel run //python:venv_link             # link .venv-bazel here for an IDE interpreter
bazel run //python/examples/monty:worker  # an example's agent worker
bazel run //python/examples/tictactoe:agents_update     # rewrite an example's agents.toml and schemas
bazel run //python/examples/tictactoe:client_sdk_update   # rewrite a committed client SDK
bazel run //contract:protocol_update      # rewrite contract/protocol.schema.json (contract/README.md)
bazel run //python/tests/conformance:goldens_update  # re-record conformance/goldens/ (conformance/README.md)
bazel run //typescript/packages/client/src:protocol_codegen_update  # rewrite its src/protocol.ts
```

- **Targets are generated.** Gazelle writes one `py_library` (or test, or binary) per `.py` file,
  with `deps` from that file's imports, so `bazel test` reruns only the tests a change can reach.
  Directives live in `python/BUILD.bazel`, which is also Gazelle's `python_root`: module names
  start there (`temporal_agent_harness.*`, `examples.*`, `tests.*`), and Gazelle gives every
  target the matching `imports`. Python sources use absolute imports only, because rules_python's
  Gazelle plugin resolves a relative import against the Bazel package path (`python.examples...`)
  rather than the `python_root`. Gazelle cannot see a module loaded by name (e.g.
  `load_agent_class("examples.x.workflow:Agent")`); those deps are added by hand with `# keep`,
  which Gazelle preserves. CI (`bazel-files` in `tests.yml`) fails when
  `just gazelle` would change anything or buildifier finds a file unformatted or with a lint
  warning, so run `just gazelle` and `just buildifier` before pushing.
- **Python** uses [aspect rules_py](https://github.com/aspect-build/rules_py) 2.x: its uv
  extension reads `uv.lock` into the `@pypi` hub (the `dev` dependency group), and it provisions
  the interpreters. The interpreter is a build setting, 3.13 by default; `--config=py3XX` selects
  another for a whole invocation.
- **Tests** are `harness_pytest_test` (`bazel/pytest.bzl`): pytest with the repo's
  `python/pyproject.toml` config, the Temporal servers pinned with checksums in `MODULE.bazel`
  (handed to `WorkflowEnvironment` by `python/tests/conftest.py`, so no test downloads one), and
  Code Mode's `monty` binary. A few tests are deselected under Bazel; each target says why.
- **Binaries** are `harness_module_binary` (`bazel/binary.bzl`), which runs a file as
  `python -m <module>` would, its module named from the `python_root`. Under `bazel run` it runs
  from the workspace root, so repo-relative paths (`.env.local`'s `TEMPORAL_CONFIG_FILE`,
  `agents.toml`) resolve as they do for the justfiles' recipes.
- **Running the examples.** The justfiles' run recipes call `bazel run` (loading `.env.local`
  first). The session manager and `serve` are `//python/temporal_agent_harness/web:cli`, which
  imports no agent code. `//python/examples:dev`, `:workers`, and `:server` are `harness_dev`
  targets (`bazel/dev.bzl`): `bazel run //python/examples:dev -- monty wiki_agent` runs the
  session manager, the server over those examples' registries, and their workers, every example
  when none are named.
  `bazel/dev_runner.py` starts them with the environment `bazel run` would give each, prefixes
  every output line with the program's name, and forwards Ctrl-C to all of them. The root
  `server`, `workers`, and `dev` recipes run these targets. Bazel holds its lock only while
  building, so separate `bazel run`s can also run side by side.
- **Agent registries are generated.** Each example declares its agents with `harness_agent`
  (`bazel/agents.bzl`): the class and the fields of its `agents.toml` entry. `harness_agent`
  writes the agent's schema document with the CLI (`schema module:Class`), and
  `harness_agent_registry` writes `agents.toml` with each entry's `workflow_type` read from that
  schema and `schema` pointing at it. Both are committed beside the BUILD file
  (`bazel run :agents_update`) and checked by `bazel test`, so `serve` reads every agent's start
  interface from them without importing agent code, under Bazel or uv alike. `nexus_hello`,
  which Bazel does not build, keeps a hand-written registry and schema.
- **Generated TypeScript** (`python/examples/*/client_sdk/`,
  `typescript/packages/client/src/protocol.ts`) is built from those schema documents
  (`harness_schema` for the protocol's) by the `harness_types` rule (`bazel/codegen.bzl`) through
  `typescript/packages/codegen` (rules_js). An example declares its clients with
  `harness_client_sdk`.
- **Committed generated files** all go through `committed_files` (`bazel/committed_files.bzl`),
  which `harness_agent_registry` and `harness_client_sdk` use too: `bazel run :<name>_update`
  writes them, and `<name>_<file>_up_to_date_test` fails while one differs from what Bazel
  generates.
  rules_js reads the workspace's `typescript/pnpm-lock.yaml`, and reruns `pnpm install
  --lockfile-only` when a workspace `package.json` changes. Each one must be listed in
  `npm_translate_lock`'s `data` in `MODULE.bazel`;
  `//typescript:npm_translate_lock_data_matches_workspace_test` fails while one is missing.
