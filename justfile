# Project-level recipes. Two ways to run the examples:
#   * ONE example standalone — from its own dir: `cd python/examples/<x> && just server|worker|client`.
#     Each example owns its justfile and is unchanged by the aggregate recipes here.
#   * ALL examples behind one UI — the aggregate recipes below: `server` merges every example's
#     agents.toml so the UI lists all agents; run each agent worker (or `just workers` to co-launch
#     them), plus the clients for the human-in-the-loop / callback agents.
# The aggregate recipes read the shared repo-root .env.local (creds: OPENAI_API_KEY, GEMINI_API_KEY,
# F1_MCP_SERVER_HOME, ...). Prerequisites + run order: see the "Run everything" section in README.md.
# The run recipes build and run with Bazel (`bazel`, via bazelisk). `server`, `workers`, and `dev`
# take example names to run a subset (`just dev monty wiki_agent`).
# Build/package + Nexus/Slack/Teams connector recipes follow the run recipes.

# Shows a notice when running from an untagged commit (silent on a release archive).
import 'scripts/untagged-notice.just'

typescript := justfile_directory() / "typescript"
ui := typescript / "apps" / "ui"
# pnpm as Bazel pins it, on Bazel's Node (//typescript:pnpm): no recipe needs Node or pnpm on
# the machine.
pnpm := "bazel run --noshow_progress --ui_event_filters=-info --show_result=0 //typescript:pnpm --"
python := justfile_directory() / "python"
monty := python / "examples" / "monty"
nexus_dir := justfile_directory() / "nexus"

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

# Minimum `temporal` CLI for the Nexus recipes. Older builds reject the dynamic config
# keys below, so fail here rather than at the first Nexus call.
temporal_cli_min := "1.9.1"

# Fail if the `temporal` CLI is missing or older than temporal_cli_min.
_require-temporal-cli:
    #!/usr/bin/env bash
    set -eu
    have=$(temporal --version 2>/dev/null | awk '{print $3}')
    if [ -z "${have}" ]; then
        echo "error: no 'temporal' CLI on PATH. Nexus needs >= {{temporal_cli_min}}: https://docs.temporal.io/cli" >&2
        exit 1
    fi
    if [ "$(printf '%s\n%s\n' "{{temporal_cli_min}}" "${have}" | sort -V | head -1)" != "{{temporal_cli_min}}" ]; then
        echo "error: temporal CLI ${have} is too old. Nexus needs >= {{temporal_cli_min}}." >&2
        exit 1
    fi

# Start a local Temporal dev server with the dynamic config Nexus needs (callbacks,
# update-with-callback, standalone Nexus operations/activities). Web UI: http://localhost:8233.
# A stock `temporal` CLI release is enough — no custom server build.
temporal-nexus: _require-temporal-cli
    temporal server start-dev \
        --dynamic-config-value 'system.maxCallbacksPerWorkflow=500' \
        --dynamic-config-value 'component.nexusoperations.callback.endpoint.template="http://localhost:7243/namespaces/{{{{.NamespaceName}}/nexus/callback"' \
        --dynamic-config-value 'callback.allowedAddresses=[{"Pattern":"*","AllowInsecure":true}]' \
        --dynamic-config-value 'history.enableUpdateCallbacks=true' \
        --dynamic-config-value 'nexusoperation.enableStandalone=true' \
        --dynamic-config-value 'activity.enableStandalone=true'

# Create/update the namespaces and Nexus endpoint needed by the chat connector.
setup-nexus:
    #!/usr/bin/env bash
    set -euo pipefail
    address=localhost:7233

    for namespace in default connector; do
        if temporal operator namespace describe --address "${address}" --namespace "${namespace}" >/dev/null 2>&1; then
            echo "Namespace ${namespace} already exists."
        else
            echo "Creating namespace ${namespace}..."
            temporal operator namespace create --address "${address}" --namespace "${namespace}"
        fi
    done

    endpoint_args=(
        --address "${address}"
        --name nexus-agent-endpoint
        --target-namespace default
        --target-task-queue nexus-agent-go
    )

    if temporal operator nexus endpoint get --address "${address}" --name nexus-agent-endpoint >/dev/null 2>&1; then
        echo "Updating Nexus endpoint nexus-agent-endpoint..."
        temporal operator nexus endpoint update "${endpoint_args[@]}"
    else
        echo "Creating Nexus endpoint nexus-agent-endpoint..."
        # The server validates the target namespace against a registry cache that
        # can lag a few seconds behind namespace creation, so retry until it lands.
        for attempt in {1..30}; do
            if temporal operator nexus endpoint create "${endpoint_args[@]}" 2>/dev/null; then
                exit 0
            fi
            sleep 1
        done
        echo "error: failed to create Nexus endpoint nexus-agent-endpoint after 30s" >&2
        # Run once more without suppressing stderr so the real error is shown.
        temporal operator nexus endpoint create "${endpoint_args[@]}"
    fi

# Run the Slack connector worker. Safe to run multiple instances.
# Requires: SLACK_BOT_TOKEN
slack-connector:
    cd "{{nexus_dir}}/ui_connector" && \
    SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN}" \
    TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}" \
    CONNECTOR_NAMESPACE="${CONNECTOR_NAMESPACE:-connector}" \
    CONNECTOR_TASK_QUEUE="${CONNECTOR_TASK_QUEUE:-nexus-connector-slack}" \
    go run ./slack/cmd/worker/

# Run the Slack webhook server.
# Requires: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET
slack-webhook:
    cd "{{nexus_dir}}/ui_connector" && \
    SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN}" \
    SLACK_SIGNING_SECRET="${SLACK_SIGNING_SECRET}" \
    TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}" \
    CONNECTOR_NAMESPACE="${CONNECTOR_NAMESPACE:-connector}" \
    CONNECTOR_TASK_QUEUE="${CONNECTOR_TASK_QUEUE:-nexus-connector-slack}" \
    go run ./slack/cmd/webhook/

# Run the Teams Connector workflow worker. Safe to run multiple instances.
teams-connector:
    cd "{{nexus_dir}}/ui_connector" && \
    TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}" \
    CONNECTOR_NAMESPACE="${CONNECTOR_NAMESPACE:-connector}" \
    CONNECTOR_TASK_QUEUE="${CONNECTOR_TASK_QUEUE:-nexus-connector-teams}" \
    go run ./teams/cmd/worker/

# Run the Python Teams SDK activity worker. Safe to run multiple instances.
# Requires: MICROSOFT_TENANT_ID, MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD
teams-activities-worker:
    cd "{{nexus_dir}}/ui_connector/teams_activity_worker" && \
    MICROSOFT_TENANT_ID="${MICROSOFT_TENANT_ID}" \
    MICROSOFT_APP_ID="${MICROSOFT_APP_ID}" \
    MICROSOFT_APP_PASSWORD="${MICROSOFT_APP_PASSWORD}" \
    TEAMS_SERVICE_URL="${TEAMS_SERVICE_URL:-}" \
    TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}" \
    CONNECTOR_NAMESPACE="${CONNECTOR_NAMESPACE:-connector}" \
    CONNECTOR_TASK_QUEUE="${CONNECTOR_TASK_QUEUE:-nexus-connector-teams}" \
    uv run python -m teams_activity_worker.worker

# Run the Teams webhook server.
teams-webhook:
    cd "{{nexus_dir}}/ui_connector" && \
    TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}" \
    CONNECTOR_NAMESPACE="${CONNECTOR_NAMESPACE:-connector}" \
    CONNECTOR_TASK_QUEUE="${CONNECTOR_TASK_QUEUE:-nexus-connector-teams}" \
    go run ./teams/cmd/webhook/

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
# Names are example directories (`monty`, `wiki_agent`); none means every example, e.g.
# `just server monty tictactoe`.
#
# Serve the named examples' agents.toml registries merged on http://localhost:8000.
server *EXAMPLES: (_run "//python/examples:server" EXAMPLES)

# Run the Svelte Vite dev server with /api proxied to the server on :8000.
ui-dev: app-install
    {{pnpm}} --dir "{{ui}}" run dev

# --- Per-example agent workers (each loads .env.local via its own justfile) ---
worker-openai-hello:
    cd "{{python}}/examples/openai_hello" && just worker

worker-pydantic:
    cd "{{python}}/examples/pydantic_ai_hello" && just worker

worker-react:
    cd "{{python}}/examples/react_agent" && just worker

worker-monty:
    cd "{{monty}}" && just worker

worker-tictactoe:
    cd "{{python}}/examples/tictactoe" && just worker

worker-auto-mode:
    cd "{{python}}/examples/auto_mode" && just worker

worker-wiki:
    cd "{{python}}/examples/callback_tools/wiki_agent" && just worker

worker-coding:
    cd "{{python}}/examples/callback_tools/coding_agent" && just worker

worker-agent-dag:
    cd "{{python}}/examples/agent_dag" && just worker

worker-code-mode-memory:
    cd "{{python}}/examples/code_mode_memory" && just worker

# Names as for `server`, e.g. `just workers monty wiki_agent`. Each output line carries its
# worker's name; Ctrl-C stops them all. Each needs its agent's prerequisites (API keys, the F1 MCP
# server, ...).
#
# Co-launch the named examples' agent workers in one terminal.
workers *EXAMPLES: (_run "//python/examples:workers" EXAMPLES)

# Against a running Temporal (`just temporal`): the session manager, `server` over the named
# examples' registries, and their workers. Names as for `server`, e.g. `just dev monty`. Each
# output line carries its program's name; Ctrl-C stops them all.
#
# Run the local stack for the named examples in one terminal.
dev *EXAMPLES: (_run "//python/examples:dev" EXAMPLES)

# `bazel run <target> -- <args>` with the repo-root .env.local loaded.
_run target *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    set -a; [ -f .env.local ] && . ./.env.local; set +a
    bazel run {{target}} -- {{ARGS}}

# --- Clients / external processes for the human-in-the-loop & callback agents ---
# ReAct client: answers the `ask_user` callback (plain chat also works in the web UI).
react-client *ARGS:
    cd "{{python}}/examples/react_agent" && just client {{ARGS}}

# Wiki client: REQUIRED — fulfills the wiki callback tools against a local dir (default: wiki/ in
# the example's directory).
wiki-client *ARGS:
    cd "{{python}}/examples/callback_tools/wiki_agent" && just client {{ARGS}}

# Coding shim: REQUIRED — the OpenCode shim that fulfills the coding callback tools (attach the
# OpenCode TUI to it). Pass a working dir, e.g. `just coding-shim ~/some/project`.
coding-shim *ARGS: (_run "//python/examples/callback_tools/coding_agent/opencode_shim:opencode_shim_bin" ARGS)

# Terminate the session-manager workflow so the next `server` start re-seeds it with the current
# (merged) registry. Needed when switching which agents are served without a fresh Temporal.
reset-manager:
    temporal workflow terminate -w session-manager || true

# Run the standalone Nexus worker exposing AgentService. AGENT_WORKFLOW_NAME is required —
# see worker.py for the rest of the env vars.
nexus-agent-worker:
    cd "{{python}}" && uv run python -m temporal_agent_harness.nexus_agent_adapter.worker

# Installs the newest nex-gen release into ~/.local/bin. Tracks latest, not a pinned
# version, while nex-gen is under active development.
install-nexgen:
    #!/usr/bin/env bash
    set -euo pipefail
    BINDIR="$HOME/.local/bin"
    OS=$(uname -s); ARCH=$(uname -m)
    case "$OS-$ARCH" in
        Darwin-arm64) TARGET=aarch64-apple-darwin ;;
        Darwin-x86_64) TARGET=x86_64-apple-darwin ;;
        Linux-aarch64) TARGET=aarch64-unknown-linux-gnu ;;
        Linux-x86_64) TARGET=x86_64-unknown-linux-gnu ;;
        *) echo "error: unsupported platform $OS-$ARCH for nex-gen"; exit 1 ;;
    esac
    TAG=$(curl -sL https://api.github.com/repos/temporalio/nex-gen/releases | grep -o '"tag_name": *"[^"]*"' | head -1 | sed -E 's/.*"([^"]+)"$/\1/')
    if [ -z "$TAG" ]; then echo "error: could not resolve latest nex-gen release from GitHub"; exit 1; fi
    if [ -x "$BINDIR/nexgen" ] && [ "$($BINDIR/nexgen --version 2>/dev/null | awk '{print $2}')" = "${TAG#v}" ]; then exit 0; fi
    URL="https://github.com/temporalio/nex-gen/releases/download/$TAG/nexgen-$TAG-$TARGET.tar.gz"
    echo "Installing nex-gen $TAG ($TARGET) to $BINDIR..."
    mkdir -p "$BINDIR" && curl -sL "$URL" | tar xz -C "$BINDIR" nexgen && chmod +x "$BINDIR/nexgen"

# Gets the contract from remote and regenerates the Python AgentService bindings.
nexus-agent-generate: install-nexgen
    "$HOME/.local/bin/nexgen" python python/temporal_agent_harness/nexus_agent_adapter/agent.nexusrpc.yaml \
        --output python/temporal_agent_harness/nexus_agent_adapter/generated

# Gets the contract from local and regenerates the Durable Tools Gateway's Python bindings.
generate-registry-contract: install-nexgen
    "$HOME/.local/bin/nexgen" python nexus/mcp/nexus_mcp/durable_tools_gateway/registry.nexusrpc.yaml \
        --output nexus/mcp/nexus_mcp/durable_tools_gateway/generated
