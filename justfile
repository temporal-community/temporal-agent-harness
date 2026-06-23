# Project-level recipes for building, checking, and packaging the harness.

ui := justfile_directory() / "ui"
monty := justfile_directory() / "examples" / "monty"

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
