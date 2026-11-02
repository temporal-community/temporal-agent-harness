# syntax=docker/dockerfile:1
#
# The image the tmprl-demo.cloud demo catalog deploys the governed analytics example from, as
# "Playpen Analytics Demo". Not needed to run the example locally (see its README); built from
# the repo root:
#   docker build -f python/examples/playpen-analytics-demo.DOCKERFILE .
#
# Nothing comes from the build context (the .dockerignore beside this sends none of it). The
# image fetches its two sources at pinned commits, so what is deployed changes only when a pin
# below does:
#   - this repo's governed analytics example, on its own branch (worktree-governed-analytics-demo);
#   - the Dogwood tool verifier the example asks before every tool call
#     (02strich/temporal-untrusted-workers, branch nexus-command-verifier).
#
# Programs, each a component's command:
#   /app/bin/cli serve python/examples/governed_analytics/agents.toml --port 8000
#   /app/bin/cli session-manager
#   /app/bin/governed-analytics-worker
#   /app/bin/governed-analytics-studio [port]   the studio page (HOST, BASE_PATH as in
#                                               demo-catalog.DOCKERFILE)
#   /app/bin/playpen-verifier                   the Dogwood verifier on the example's policy
#
# Every component reads the Temporal connection from TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE and
# TEMPORAL_API_KEY. The worker also needs GEMINI_API_KEY. The agent asks the verifier through
# the Nexus endpoint `playpen-verifier`, which has to target the verifier's task queue,
# `tool-verifier`. The verifier keeps its store (grants and every session's history) on its own
# disk, under /home/app/dogwood.
FROM debian:bookworm-slim

ARG HARNESS_REPO=https://github.com/temporal-community/temporal-agent-harness.git
ARG HARNESS_COMMIT=d840a801dc29da77336ac1880eb303be24ee62b4
ARG VERIFIER_REPO=https://github.com/02strich/temporal-untrusted-workers.git
ARG VERIFIER_COMMIT=2d52ae208ba9aa1f37b645bcdf40c1c88040d9a6
# The verifier's workspace needs Rust 1.92 (edition 2024).
ARG RUST_VERSION=1.92.0

# Bazelisk for the architecture this builds on; protoc for the Temporal Rust SDK's protos.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git build-essential python3 pkg-config protobuf-compiler \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /usr/local/bin/bazel \
      "https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-$(dpkg --print-architecture)" \
 && chmod +x /usr/local/bin/bazel \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /app/bin /home/app/dogwood && chown -R app /app /home/app/dogwood

USER app
WORKDIR /home/app
ENV PATH=/home/app/.cargo/bin:$PATH
RUN curl --proto '=https' -sSf https://sh.rustup.rs \
      | sh -s -- -y --profile minimal --default-toolchain "$RUST_VERSION"

# Each source at exactly its pinned commit, without the rest of its history.
RUN set -eu; \
    fetch() { git init -q "$1"; git -C "$1" fetch -q --depth 1 "$2" "$3"; git -C "$1" checkout -q FETCH_HEAD; }; \
    fetch src "$HARNESS_REPO" "$HARNESS_COMMIT"; \
    fetch verifier "$VERIFIER_REPO" "$VERIFIER_COMMIT"

RUN cd verifier/verifiers \
 && cargo build --release --locked -p dogwood-verifier \
 && cp target/release/dogwood-verifier /app/bin/ \
 && rm -rf target /home/app/.cargo/registry

# The example's programs, recorded as `bazel run` scripts (see demo-catalog.DOCKERFILE), and its
# three DuckDB databases, written into the example's data/ (the same data every build).
WORKDIR /home/app/src
RUN set -eu; \
    run() { bazel run --script_path="/app/bin/$1" "$2"; }; \
    run cli //python/temporal_agent_harness/web:cli; \
    run governed-analytics-worker //python/examples/governed_analytics:worker; \
    run governed-analytics-studio //python/examples/governed_analytics/ui:studio; \
    bazel run //python/examples/governed_analytics:generate_data; \
    bazel shutdown

COPY --chmod=755 <<'EOF' /app/bin/playpen-verifier
#!/bin/sh
# The Dogwood verifier, enforcing the governed analytics example's policy. It takes
# TEMPORAL_ADDRESS as a URL; a bare host:port (as the catalog gives it) gets https:// with an API
# key and http:// without.
set -eu
case "${TEMPORAL_ADDRESS:-}" in
  "" | *://*) ;;
  *) if [ -n "${TEMPORAL_API_KEY:-}" ]; then scheme=https; else scheme=http; fi
     export TEMPORAL_ADDRESS="$scheme://$TEMPORAL_ADDRESS" ;;
esac
policies=/home/app/src/python/examples/governed_analytics/policies
export DOGWOOD_POLICY_FILE="${DOGWOOD_POLICY_FILE:-$policies/bank.dw}"
export DOGWOOD_SCHEMA_FILE="${DOGWOOD_SCHEMA_FILE:-$policies/schema.cedarschema}"
export DOGWOOD_EVENT_SCHEMA_FILE="${DOGWOOD_EVENT_SCHEMA_FILE:-$policies/events.dwschema}"
export DOGWOOD_STORE="${DOGWOOD_STORE:-/home/app/dogwood/bank.redb}"
exec /app/bin/dogwood-verifier
EOF

# The studio's page server binds loopback by default; in a container it has to listen on eth0.
ENV HOST=0.0.0.0
CMD ["/app/bin/cli", "--help"]
