# Project-level recipes. Two ways to run the examples:
#   * ONE example standalone — from its own dir: `cd python/examples/<x> && just server|worker|client`.
#     Each example owns its justfile and is unchanged by the aggregate recipes here.
#   * ALL examples behind one UI — the aggregate recipes below: `server` merges every example's
#     agents.toml so the UI lists all agents; run each agent worker (or `just workers` to co-launch
#     them), plus the client for the human-in-the-loop ReAct agent.
# The aggregate recipes read the shared repo-root .env.local (creds: OPENAI_API_KEY, GEMINI_API_KEY,
# F1_MCP_SERVER_HOME, ...). Prerequisites + run order: see the "Run everything" section in README.md.
# The run recipes build and run with Bazel (`bazel`, via bazelisk). `server`, `workers`, and `dev`
# take example names to run a subset (`just dev monty auto_mode`).
# Build/package recipes come first.

# Shows a notice when running from an untagged commit (silent on a release archive).
import 'scripts/untagged-notice.just'

typescript := justfile_directory() / "typescript"
ui := typescript / "apps" / "ui"
# pnpm as Bazel pins it, on Bazel's Node (//typescript:pnpm): no recipe needs Node or pnpm on
# the machine.
pnpm := "bazel run --noshow_progress --ui_event_filters=-info --show_result=0 //typescript:pnpm --"
python := justfile_directory() / "python"

# List available recipes.
default:
    @just --list

# Install the JavaScript workspace (typescript/), which includes the Svelte UI, for `ui-dev`.
app-install:
    {{pnpm}} --dir "{{typescript}}" install --frozen-lockfile

# Type-check the Svelte UI and run its test suite.
app-check:
    bazel test //typescript/apps/ui/...

# Releases publish from CI (.github/workflows/publish.yml), which runs the same targets.
#
# Test the Python package and the console, then build the wheel and sdist (//python:dists).
package:
    bazel test //python/... //typescript/apps/ui/...
    bazel build //python:dists

# After adding, moving, or changing the imports of a .py file, or changing uv.lock. CI fails while
# either is out of date.
#
# Regenerate the Bazel build: gazelle_python.yaml from uv.lock, then every Python BUILD target.
gazelle:
    bazel run //python:gazelle_python_manifest.update
    bazel run //:gazelle

# CI fails on anything this would change, and on lint warnings it cannot fix.
#
# Format every Bazel file and apply buildifier's lint fixes.
buildifier:
    bazel run //:buildifier

# ===== Run ALL example agents behind one UI (each example is still runnable from its own dir) =====

# Start a local Temporal dev server (Web UI: http://localhost:8233; needs the `temporal` CLI).
# Start this fresh, or run `just reset-manager` before `server`, so the merged registry takes effect.
temporal:
    temporal server start-dev

# Run the packaged, agent-agnostic session-manager worker (hosts only SessionManagerWorkflow).
session-manager: (_run "//python/temporal_agent_harness/web:cli" "session-manager")

# Serves the console as Bazel builds it from typescript/apps/ui/, so a change there shows up on the
# next run. An agent only runs if its worker is up — see the worker recipes below.
#
# Names are example directories (`monty`, `auto_mode`); none means every example, e.g.
# `just server monty auto_mode`.
#
# Serve the named examples' agents.toml registries merged on http://localhost:8000.
server *EXAMPLES: (_run "//python/examples:server" EXAMPLES)

# Run the Svelte Vite dev server with /api proxied to the server on :8000.
ui-dev: app-install
    {{pnpm}} --dir "{{ui}}" run dev

# Names as for `server`, e.g. `just workers monty auto_mode`. Each output line carries its
# worker's name; Ctrl-C stops them all. Each needs its agent's prerequisites (API keys, the F1 MCP
# server, ...).
#
# Co-launch the named examples' agent workers in one terminal.
workers *EXAMPLES: (_run "//python/examples:workers" EXAMPLES)

# Against a running Temporal (`just temporal`): the session manager, `server` over the named
# examples' registries, and their workers. Names as for `server`, e.g. `just dev monty auto_mode`. Each
# output line carries its program's name; Ctrl-C stops them all.
#
# Run the local stack for the named examples in one terminal.
dev *EXAMPLES: (_run "//python/examples:dev" EXAMPLES)

# The next `server` start re-seeds it with the current (merged) registry. Needed when switching
# which agents are served without a fresh Temporal.
#
# Terminate the session-manager workflow.
reset-manager:
    temporal workflow terminate -w session-manager || true

# `bazel run <target> -- <args>` with the repo-root .env.local loaded.
_run target *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    set -a; [ -f .env.local ] && . ./.env.local; set +a
    bazel run {{target}} -- {{ARGS}}

# ReAct client: answers the `ask_user` callback (plain chat also works in the web UI).
react-client *ARGS:
    cd "{{python}}/examples/react_agent" && just client {{ARGS}}

# Needs `server` and the agent_dag worker running. `port` defaults to 5173.
#
# Serve the agent_dag example's studio UI on http://127.0.0.1:<port>.
agent-dag-studio port="":
    bazel run //python/examples/agent_dag/ui:studio -- {{port}}

# Needs `server` and the tictactoe worker running. `port` defaults to 5173; pass another to run it
# beside the studio, e.g. `just tictactoe-play 5174`.
#
# Serve the tictactoe example's Svelte board on http://127.0.0.1:<port>.
tictactoe-play port="":
    bazel run //python/examples/tictactoe/ui:play -- {{port}}
