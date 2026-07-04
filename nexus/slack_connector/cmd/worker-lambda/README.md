# worker-lambda

Runs the Slack connector Temporal worker inside AWS Lambda using the
[`lambdaworker`](https://pkg.go.dev/go.temporal.io/sdk/contrib/aws/lambdaworker)
contrib package. It registers the exact same workflow and activities as the
always-on `cmd/worker` (via the shared `connectorworker.Register`), but delegates
the worker lifecycle to Lambda: on each invocation it dials Temporal, polls for
tasks until near the invocation deadline, then drains and shuts down.

This is intended as an **alternative** to `cmd/worker`, not a co-poller. Don't run
both against the same task queue in the same namespace — the Lambda worker is
versioned (Worker Deployment Versioning) and the always-on worker is not, which
can route tasks in surprising ways.

## Configuration

Connection settings are loaded by
[`envconfig`](https://pkg.go.dev/go.temporal.io/sdk/contrib/envconfig) from
environment variables and/or an optional `temporal.toml`. The config file is
resolved from `TEMPORAL_CONFIG_FILE`, then `temporal.toml` under
`$LAMBDA_TASK_ROOT` (`/var/task`), then the working directory.

| Variable | Purpose | Default |
|---|---|---|
| `SLACK_BOT_TOKEN` | Slack bot token (required). Use Secrets Manager in prod. | — |
| `TEMPORAL_ADDRESS` | Temporal frontend address | (envconfig default) |
| `TEMPORAL_NAMESPACE` | Namespace | `connector` |
| `TEMPORAL_TASK_QUEUE` | Task queue | `nexus-connector-slack` |
| `TEMPORAL_API_KEY` / `TEMPORAL_TLS*` | Auth / TLS for Temporal Cloud | — |
| `WORKER_DEPLOYMENT_NAME` | Deployment name for versioning | `nexus-connector-slack` |
| `WORKER_BUILD_ID` | Build ID for versioning (set to the git SHA in CI) | `dev` |

The namespace and task-queue defaults match `cmd/worker` so behavior is
consistent unless explicitly overridden.

## Versioning

`lambdaworker` always enables Worker Deployment Versioning. This binary sets the
default versioning behavior to **Pinned** — connector workflows are short-lived,
so pinning avoids migrating any in-flight run onto a new build.

Because it's Pinned, each new `WORKER_BUILD_ID` must be made the deployment's
current version on the server or tasks won't route to it:

```
temporal worker deployment set-current-version \
  --deployment-name nexus-connector-slack --build-id <WORKER_BUILD_ID>
```

## Build & deploy

Build a Linux binary named `bootstrap` and deploy on the `provided.al2023`
runtime:

```
GOOS=linux GOARCH=arm64 go build -o bootstrap ./cmd/worker-lambda   # use amd64 for x86_64 Lambdas
zip function.zip bootstrap
# create/update the Lambda function with runtime=provided.al2023, handler=bootstrap
```

## Invocation cadence (important)

A Lambda worker only polls for tasks **while an invocation is running**, then
drains before the deadline. To behave like an always-on worker, invoke it on a
tight recurring schedule — e.g. an EventBridge rule every ~1 minute — with the
function timeout set to at least the longest activity `StartToClose` plus the
shutdown buffer (~7s). A **minimum 60s timeout** is recommended.

For backlog-driven scaling of serverless workers, see Temporal's
[`auto-scaled-workers`](https://github.com/temporalio/sdk-go) project — out of
scope for this binary.

## Observability (optional)

Metrics/tracing are opt-in via the
[`lambdaworker/otel`](https://pkg.go.dev/go.temporal.io/sdk/contrib/aws/lambdaworker/otel)
sub-package (AWS Distro for OpenTelemetry). Not wired up here.
