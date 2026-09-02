# Project-level recipes. Two ways to run the examples:
#   * ONE example standalone — from its own dir: `cd examples/<x> && just server|worker|client`.
#     Each example owns its justfile and is unchanged by the aggregate recipes here.
#   * ALL examples behind one UI — the aggregate recipes below: `server` merges every example's
#     agents.toml so the UI lists all agents; run each agent worker (or `just workers` to co-launch
#     them), plus the clients for the human-in-the-loop / callback agents.
# The aggregate recipes read the shared repo-root .env.local (creds: OPENAI_API_KEY, GEMINI_API_KEY,
# F1_MCP_SERVER_HOME, ...). Prerequisites + run order: see the "Run everything" section in README.md.
# Build/package + Nexus/Slack/Teams connector recipes follow the run recipes.

ui := justfile_directory() / "ui"
monty := justfile_directory() / "examples" / "monty"
nexus_dir := justfile_directory() / "nexus"
devserver_dir := nexus_dir / "devserver"
build_dir := justfile_directory() / ".build"

# List available recipes.
default:
    @just --list

# Install the Svelte UI dependencies.
app-install:
    pnpm --dir "{{ui}}" install

# Type-check the Svelte UI, then run every self-check in ui/scripts. The loop is a glob rather than a
# list so a new check runs the day it lands; `failglob` means an empty scripts dir is an error rather
# than a silent pass, which is the one way a glob could be worse than naming each file.
app-check:
    #!/usr/bin/env bash
    set -euo pipefail
    shopt -s failglob
    pnpm --dir "{{ui}}" run check
    for check in "{{ui}}"/scripts/check-*.mjs; do
        echo "--- ${check##*/}"
        node "${check}"
    done

# Build the Svelte UI into temporal_agent_harness/ui/dist.
app-build:
    pnpm --dir "{{ui}}" run build

# Build, test, and create the wheel/sdist in dist/.
package: app-build app-check
    uv run pytest
    uv build

# Start the custom Temporal server with Nexus callback/update dynamic config enabled.
temporal-latest:
    #!/usr/bin/env bash
    set -euo pipefail

    temporal_build_dir="{{build_dir}}/temporal-src"
    rm -rf "${temporal_build_dir}"
    mkdir -p "${temporal_build_dir}"

    echo "Cloning temporalio/temporal@main..."
    git clone --depth=1 https://github.com/temporalio/temporal.git "${temporal_build_dir}"

    echo "Building temporal-server binary..."
    cd "${temporal_build_dir}"
    GOWORK=off GOFLAGS= go build -o "{{devserver_dir}}/temporal-server" ./cmd/server

    rm -rf "${temporal_build_dir}"
    echo "Built: {{devserver_dir}}/temporal-server"

    cd "{{devserver_dir}}"
    ./temporal-server --config-file config.yaml --allow-no-auth start

# Start Temporal UI on http://localhost:8233 and point it at the custom server.
temporal-latest-ui:
    docker run --rm -p 8233:8080 \
        -e TEMPORAL_ADDRESS=host.docker.internal:7233 \
        temporalio/ui

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

# Run the shared, agent-agnostic session-manager worker (hosts only SessionManagerWorkflow).
session-manager:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    set -a; [ -f .env.local ] && . ./.env.local; set +a
    uv run --group examples python -m examples.session_manager_worker

# Build the UI, then serve EVERY example's agents.toml merged on http://localhost:8000, so the UI
# lists all agents. (An agent only runs if its worker is up — see the worker recipes below.)
server: app-build
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    set -a; [ -f .env.local ] && . ./.env.local; set +a
    uv run --group examples python -m examples.app \
        examples/openai_hello/agents.toml \
        examples/pydantic_ai_hello/agents.toml \
        examples/react_agent/agents.toml \
        examples/monty/agents.toml \
        examples/callback_tools/wiki_agent/agents.toml \
        examples/callback_tools/coding_agent/agents.toml \
        --host 0.0.0.0 --port 8000

# Run the Svelte Vite dev server with /api proxied to the server on :8000.
ui-dev:
    pnpm --dir "{{ui}}" run dev

# --- Per-example agent workers (each loads .env.local via its own justfile) ---
worker-openai-hello:
    cd "{{justfile_directory()}}/examples/openai_hello" && just worker

worker-pydantic:
    cd "{{justfile_directory()}}/examples/pydantic_ai_hello" && just worker

worker-react:
    cd "{{justfile_directory()}}/examples/react_agent" && just worker

worker-monty:
    cd "{{monty}}" && just worker

worker-wiki:
    cd "{{justfile_directory()}}/examples/callback_tools/wiki_agent" && just worker

worker-coding:
    cd "{{justfile_directory()}}/examples/callback_tools/coding_agent" && just worker

# Co-launch all six agent workers in one terminal (Ctrl-C stops them all; logs interleave).
# Requires every agent's prerequisites at once (both API keys, the F1 MCP server, etc.).
workers:
    #!/usr/bin/env bash
    set -euo pipefail
    # Stop every worker on Ctrl-C / TERM. Trap the signals explicitly so cleanup runs regardless of
    # what the shell is blocked on; clear the traps first so `kill 0` (SIGTERM to the whole process
    # group, incl. this shell) runs once. (Ctrl-C also reaches the workers directly via the
    # foreground process group; this makes the intent explicit and cleans up any straggler.)
    trap 'trap - INT TERM EXIT; kill 0' INT TERM EXIT
    just worker-openai-hello &
    just worker-pydantic &
    just worker-react &
    just worker-monty &
    just worker-wiki &
    just worker-coding &
    wait

# --- Clients / external processes for the human-in-the-loop & callback agents ---
# ReAct client: answers the `ask_user` callback (plain chat also works in the web UI).
react-client *ARGS:
    cd "{{justfile_directory()}}/examples/react_agent" && just client {{ARGS}}

# Wiki client: REQUIRED — fulfills the wiki callback tools against a local dir (default ./wiki).
wiki-client *ARGS:
    cd "{{justfile_directory()}}/examples/callback_tools/wiki_agent" && just client {{ARGS}}

# Coding shim: REQUIRED — the OpenCode shim that fulfills the coding callback tools (attach the
# OpenCode TUI to it). Pass a working dir, e.g. `just coding-shim ~/some/project`.
coding-shim *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{justfile_directory()}}"
    uv run --group examples python -m examples.callback_tools.coding_agent.opencode_shim {{ARGS}}

# Terminate the session-manager workflow so the next `server` start re-seeds it with the current
# (merged) registry. Needed when switching which agents are served without a fresh Temporal.
reset-manager:
    temporal workflow terminate -w session-manager || true

# Run the standalone Nexus worker exposing AgentService. AGENT_WORKFLOW_NAME is required —
# see worker.py for the rest of the env vars.
nexus-agent-worker:
    uv run python -m temporal_agent_harness.nexus_agent_adapter.worker

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
    "$HOME/.local/bin/nexgen" python temporal_agent_harness/nexus_agent_adapter/agent.nexusrpc.yaml \
        --output temporal_agent_harness/nexus_agent_adapter/generated

# Gets the contract from local and regenerates the Durable Tools Gateway's Python bindings.
generate-registry-contract: install-nexgen
    "$HOME/.local/bin/nexgen" python nexus/mcp/durable_tools_gateway/registry.nexusrpc.yaml \
        --output nexus/mcp/durable_tools_gateway/generated
