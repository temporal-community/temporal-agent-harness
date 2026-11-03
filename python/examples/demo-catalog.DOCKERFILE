# The image the tmprl-demo.cloud demo catalog deploys these examples from. Not needed to run the
# examples locally (see the README); built from the repo root:
#   docker build -f python/examples/demo-catalog.DOCKERFILE .
#
# One image for every harness process: the session manager, `cli serve`, each example's worker,
# and the agent_dag studio page. The repo builds with Bazel, so this builds the targets once and
# records each `bazel run` as a script under /app/bin (`--script_path`), which runs it later
# without Bazel. The scripts point into Bazel's output base, so this stays a single stage. Pick a
# program with the component's command, e.g.
#   /app/bin/cli serve <agents.toml ...> --host 0.0.0.0 --port 8000
#   /app/bin/cli session-manager
#   /app/bin/py-openai_hello-worker
#
# Each deployment sets these, the same in every component:
#   TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, TEMPORAL_API_KEY   the Temporal connection
#   TASK_QUEUE_PREFIX   puts every worker's queue, the server's routing to them, and the session
#                       manager's workflow id and queue under one prefix. A crashable workspace
#                       sets it with `crashable.taskQueueEnv: TASK_QUEUE_PREFIX`.
# and per component:
#   BASE_PATH        agent-dag-studio: serve the page under this path prefix (the catalog's
#                    ingress does not strip it)
#   TEMPORAL_UI_URL  cli serve: the Temporal Web UI to link to, when it is neither Temporal
#                    Cloud's nor a local dev server's
FROM debian:bookworm-slim

# Bazelisk for the architecture this builds on, so a build on Apple Silicon runs natively.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git build-essential python3 \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL -o /usr/local/bin/bazel \
      "https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-$(dpkg --print-architecture)" \
 && chmod +x /usr/local/bin/bazel \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /app/bin && chown -R app /app

USER app
WORKDIR /home/app/src
COPY --chown=app . .

RUN set -eu; \
    run() { bazel run --script_path="/app/bin/$1" "$2"; }; \
    run cli //python/temporal_agent_harness/web:cli; \
    for ex in agent_dag code_mode_memory monty openai_hello pydantic_ai_hello; do \
      run "py-$ex-worker" "//python/examples/$ex:worker"; \
    done; \
    run ts-monty-worker //typescript/examples/monty:worker; \
    run ts-openai-hello-worker //typescript/examples/openai_hello:worker; \
    run agent-dag-studio //python/examples/agent_dag/ui:studio; \
    bazel shutdown

# The studio's page server binds loopback by default; in a container it has to listen on eth0.
ENV HOST=0.0.0.0
CMD ["/app/bin/cli", "--help"]
