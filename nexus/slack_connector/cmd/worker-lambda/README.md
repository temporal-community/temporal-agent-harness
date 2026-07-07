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
| `TEMPORAL_API_KEY_SECRET_ARN` | ARN of a Secrets Manager secret (plain string) with the Temporal Cloud API key. When set, it's fetched at cold start and installed as API-key credentials with TLS on. | — |
| `SLACK_BOT_TOKEN_SECRET_ARN` | ARN of a Secrets Manager secret (plain string) with the Slack bot token. Falls back to `SLACK_BOT_TOKEN` if unset. | — |
| `SLACK_BOT_TOKEN` | Slack bot token, used only when `SLACK_BOT_TOKEN_SECRET_ARN` is unset (local/dev). | — |
| `TEMPORAL_ADDRESS` | Temporal frontend address | (envconfig default) |
| `TEMPORAL_NAMESPACE` | Namespace | `connector` |
| `TEMPORAL_TASK_QUEUE` | Task queue | `nexus-connector-slack` |
| `TEMPORAL_API_KEY` / `TEMPORAL_TLS*` | Direct auth / TLS (used when `TEMPORAL_API_KEY_SECRET_ARN` is unset) | — |
| `WORKER_DEPLOYMENT_NAME` | Deployment name for versioning | `nexus-connector-slack` |
| `WORKER_BUILD_ID` | Build ID for versioning (set to the git SHA in CI) | `dev` |

The namespace and task-queue defaults match `cmd/worker` so behavior is
consistent unless explicitly overridden. In production the API key and Slack
token come from Secrets Manager via the `*_SECRET_ARN` vars; envconfig's direct
`TEMPORAL_API_KEY` / `SLACK_BOT_TOKEN` are the local/dev fallback.

**For a full AWS deploy (CloudFormation + Temporal Cloud registration), see
[`../deploy/README-lambda.md`](../deploy/README-lambda.md).**

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

> **Set `WORKER_BUILD_ID` to the git SHA (or another unique per-build value) in
> CI.** It defaults to `"dev"`; if you deploy changed code without changing the
> build ID, Pinned versioning re-pins workflows onto a build ID whose code has
> silently changed — defeating the point of pinning. One build ID must map to
> one immutable build.

## Build & deploy

Build the arm64 `bootstrap` artifact with the Makefile target:

```
make build-lambda   # -> build/worker-lambda/worker-lambda.zip (provided.al2023, arm64)
```

The full AWS + Temporal Cloud deploy (CloudFormation stacks, secrets, Worker
Deployment registration) is in [`../deploy/README-lambda.md`](../deploy/README-lambda.md).

## Invocation (handled by Temporal Cloud)

With Temporal Cloud Serverless Workers you don't schedule invocations — **Temporal
Cloud invokes the function when tasks arrive**, assuming an IAM role in your
account (see `../deploy/temporal-cloud-serverless-worker-role.yaml`). Each
invocation polls, processes tasks, then drains before the deadline.

Set the function **timeout** to at least the longest activity `StartToClose` plus
the shutdown buffer (~7s); a **60s minimum** is recommended.

> **Streaming is the part that fits Lambda's model least.** The connector's
> `Stream` activity streams agent output back over the life of a turn. If a turn
> runs longer than the invocation's remaining time, the worker drains and the
> activity is cut mid-stream, then retried on a fresh invocation. Size the
> function timeout for your longest expected turn, and be aware that very
> long-running or open-ended streams are a poor fit for per-invocation Lambda
> execution — the always-on `cmd/worker` is better suited to those.

## Observability (optional)

Metrics/tracing are opt-in via the
[`lambdaworker/otel`](https://pkg.go.dev/go.temporal.io/sdk/contrib/aws/lambdaworker/otel)
sub-package (AWS Distro for OpenTelemetry). Not wired up here.
