# Project-level recipes for building, checking, and packaging the harness. Each example owns its
# own justfile (its server / worker / registry / recipes); this top-level file stays lean and
# just delegates the Monty stack + build/package. The shared repo-root .env.local is read by each
# example's justfile via dotenv-path.

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

# Type-check the Svelte UI and run the local Svelte 5 syntax guard.
app-check:
    pnpm --dir "{{ui}}" run check
    pnpm --dir "{{ui}}" run check:svelte5

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
    cd "{{nexus_dir}}/messaging_connector" && \
    SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN}" \
    TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}" \
    CONNECTOR_NAMESPACE="${CONNECTOR_NAMESPACE:-connector}" \
    CONNECTOR_TASK_QUEUE="${CONNECTOR_TASK_QUEUE:-nexus-connector-slack}" \
    go run ./cmd/slack/worker/

# Run the Teams Connector workflow worker. Safe to run multiple instances.
teams-connector:
    cd "{{nexus_dir}}/messaging_connector" && \
    TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}" \
    CONNECTOR_NAMESPACE="${CONNECTOR_NAMESPACE:-connector}" \
    CONNECTOR_TASK_QUEUE="${CONNECTOR_TASK_QUEUE:-nexus-connector-teams}" \
    go run ./cmd/teams/worker/

# Run the Python Teams SDK activity worker. Safe to run multiple instances.
# Requires: MICROSOFT_TENANT_ID, MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD
teams-py-worker:
    cd "{{nexus_dir}}/messaging_connector/teams_activity_worker" && \
    MICROSOFT_TENANT_ID="${MICROSOFT_TENANT_ID}" \
    MICROSOFT_APP_ID="${MICROSOFT_APP_ID}" \
    MICROSOFT_APP_PASSWORD="${MICROSOFT_APP_PASSWORD}" \
    TEAMS_SERVICE_URL="${TEAMS_SERVICE_URL:-}" \
    TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}" \
    CONNECTOR_NAMESPACE="${CONNECTOR_NAMESPACE:-connector}" \
    CONNECTOR_TASK_QUEUE="${CONNECTOR_TASK_QUEUE:-nexus-connector-teams}" \
    uv run python -m teams_activity_worker

# Run the Slack webhook server.
# Requires: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET
slack-webhook:
    cd "{{nexus_dir}}/messaging_connector" && \
    SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN}" \
    SLACK_SIGNING_SECRET="${SLACK_SIGNING_SECRET}" \
    TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}" \
    CONNECTOR_NAMESPACE="${CONNECTOR_NAMESPACE:-connector}" \
    CONNECTOR_TASK_QUEUE="${CONNECTOR_TASK_QUEUE:-nexus-connector-slack}" \
    go run ./cmd/slack/webhook/

# Run the Teams webhook server.
teams-webhook:
    cd "{{nexus_dir}}/messaging_connector" && \
    TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}" \
    CONNECTOR_NAMESPACE="${CONNECTOR_NAMESPACE:-connector}" \
    CONNECTOR_TASK_QUEUE="${CONNECTOR_TASK_QUEUE:-nexus-connector-teams}" \
    go run ./cmd/teams/webhook/

# Start a local Temporal dev server for the Monty example.
temporal:
    cd "{{monty}}" && just temporal

# Run the packaged session-manager worker for the Monty example.
session-manager:
    cd "{{monty}}" && just session-manager

# Build and serve the Svelte UI + FastAPI API on http://localhost:8000.
server:
    cd "{{monty}}" && just server

# Run the Svelte Vite dev server with /api proxied to the server on :8000.
ui-dev:
    cd "{{monty}}" && just ui-dev

# Run the Monty example agent worker.
monty-worker:
    cd "{{monty}}" && just worker
