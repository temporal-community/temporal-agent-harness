# worker-lambda

Runs the agent-adapter **Nexus worker** inside AWS Lambda using the
[`lambdaworker`](https://pkg.go.dev/go.temporal.io/sdk/contrib/aws/lambdaworker)
contrib package. It registers the same `AgentService` Nexus handler as the
always-on `cmd/main.go`, but delegates the worker lifecycle to Lambda: Temporal
Cloud invokes the function when Nexus tasks arrive, the worker dials Temporal,
serves the operations, then drains before the invocation deadline.

This is a **Nexus-only** worker (`DisableWorkflowWorker`) — it serves the
`sendAgentMessage` / `pollMessages` / etc. operations, but the agent workflow
itself still runs on the **Python** worker (`AGENT_TASK_QUEUE`). It is intended
as an alternative to the always-on `cmd/main.go`; don't run both against the same
Nexus task queue in the same namespace.

## Why this fits Lambda (and the one caveat)

Every `AgentService` operation is a short request/response handler — even the
async `pollMessages`: its `Start` attaches an update-with-callback and returns a
token immediately, and the eventual completion is delivered by Temporal Cloud to
the caller's callback URL, **not** by this worker. So the worker only needs to be
alive long enough to run `Start` — a good fit for per-invocation Lambda.

> **Caveat (verify on first deploy).** Temporal Cloud Serverless Workers is
> pre-release, and the docs don't explicitly state whether the Worker Controller
> triggers a Lambda invocation for **Nexus** tasks (vs. only workflow/activity
> tasks). The Go SDK ships Nexus poller defaults for Lambda
> (`MaxConcurrentNexusTaskPollers`) and `Options.RegisterNexusService` exists, so
> it's clearly intended — but the invocation trigger can only be confirmed
> empirically: register the endpoint, trigger one call, and confirm a CloudWatch
> invocation. If it never fires, fall back to the always-on `cmd/main.go` on
> long-running compute (ECS/Fargate).

## Configuration

Connection settings load via
[`envconfig`](https://pkg.go.dev/go.temporal.io/sdk/contrib/envconfig) from env
vars / an optional `temporal.toml` (resolved from `TEMPORAL_CONFIG_FILE`, then
`$LAMBDA_TASK_ROOT/temporal.toml`, then the working dir).

| Variable | Purpose | Default |
|---|---|---|
| `AGENT_WORKFLOW_NAME` | Registered agent workflow type name. **Required** — the worker exits at startup if unset. | — |
| `AGENT_TASK_QUEUE` | Task queue the agent workflow runs on (the Python worker's queue). | `agent` |
| `AGENT_WORKFLOW_ID_PREFIX` | Prefix prepended to the session ID to form the agent workflow ID. | `agent-` |
| `TEMPORAL_API_KEY_SECRET_ARN` | ARN of a Secrets Manager secret (plain string) with the Temporal Cloud API key. When set, it's fetched at cold start and installed as API-key credentials with TLS on. | — |
| `TEMPORAL_ADDRESS` | Temporal frontend address | (envconfig default) |
| `TEMPORAL_NAMESPACE` | Namespace where the agent workflow runs and this worker polls. Must match the Nexus endpoint target namespace. | (envconfig default) |
| `TEMPORAL_TASK_QUEUE` | Nexus task queue this worker listens on (the endpoint target). | `nexus-agent-go` |
| `TEMPORAL_API_KEY` / `TEMPORAL_TLS*` | Direct auth / TLS (used when `TEMPORAL_API_KEY_SECRET_ARN` is unset) | — |
| `WORKER_DEPLOYMENT_NAME` | Deployment name for versioning | `nexus-agent-go` |
| `WORKER_BUILD_ID` | Build ID for versioning (set to the git SHA in CI) | `dev` |

Unlike the connector worker, this binary needs **no Slack secret** — it only
talks to Temporal.

## Build & deploy

```
make build-lambda   # -> build/worker-lambda/worker-lambda.zip (provided.al2023, arm64)
```

The full AWS + Temporal Cloud deploy (CloudFormation, secrets, Worker Deployment
registration, **Nexus endpoint registration**) is in
[`../../deploy/README-lambda.md`](../../deploy/README-lambda.md).
