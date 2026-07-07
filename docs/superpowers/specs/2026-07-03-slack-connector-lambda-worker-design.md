# Serverless Slack Connector Worker on AWS Lambda

**Date:** 2026-07-03
**Status:** Approved design, ready for implementation plan

## Goal

Make the Slack connector Temporal worker (`nexus/slack_connector/cmd/worker/main.go`)
runnable serverlessly on AWS Lambda, using Temporal's `lambdaworker` contrib package,
without disturbing the existing always-on worker binary.

## Key findings that shape the design

- **`lambdaworker` is a separate contrib module**, not part of the main SDK:
  `go.temporal.io/sdk/contrib/aws/lambdaworker` (latest `v0.1.1`). Adding it pulls in
  `github.com/aws/aws-lambda-go` and `go.temporal.io/sdk/contrib/envconfig`. A throwaway
  compile spike confirmed it resolves and builds cleanly against the project's SDK.
- **`RunWorker` owns the entire per-invocation lifecycle**: it dials the Temporal client
  itself (via `envconfig`), creates a Lambda-tuned worker, polls for tasks until near the
  invocation deadline, then gracefully drains and shuts down. It never returns under normal
  operation. There is therefore no clean "shared middle" to branch around inside the
  existing `main`.
- **The `configure` callback runs once at init**; its registrations are replayed onto a
  fresh worker on every invocation. Building the Slack bot / driver inside the callback is
  correct and is done once per cold start (not per invocation).
- **Worker Deployment Versioning is always ON** for `RunWorker`. It requires a
  `worker.WorkerDeploymentVersion{DeploymentName, BuildID}` and a versioning behavior. The
  existing worker runs unversioned.
- **Config source differs.** The Lambda path gets connection settings from `envconfig`
  (`TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE`, `TEMPORAL_API_KEY`,
  `TEMPORAL_TLS*`, optional `temporal.toml`), not from the existing `client.Dial` +
  `CONNECTOR_*` scheme.

## Decisions

- **Structure:** a new, separate binary `nexus/slack_connector/cmd/worker-lambda/main.go`.
  (Not an env-var branch inside the existing `main` — the two run paths share almost
  nothing and branching would drag `aws-lambda-go` into the standard worker's build.)
- **Deployment model:** the Lambda worker is an **alternative / replacement** for the
  always-on worker, not a co-poller. The two are not expected to poll the same task queue
  in the same namespace simultaneously (which avoids versioned/unversioned task-routing
  surprises).
- **Versioning behavior:** **Pinned** (`workflow.VersioningBehaviorPinned`) as the default.
- **SDK upgrade:** _abandoned._ The upgrade was explored (`v1.41.1` → `v1.45.0`) but dropped
  because it worsened the api dependency conflict described below (SDK v1.45.0 floors
  `go.temporal.io/api` at v1.62.12) for no benefit to the Lambda work. The Lambda worker
  runs on the existing SDK v1.41.1.

## Dependency conflict: the custom `go.temporal.io/api` build

The whole `nexus` workspace pins a **custom, unreleased `go.temporal.io/api` build**:
`v1.62.3-0.20260330144107-1e2b1facde20`. That pseudo-version is the head of the
`update-callback` branch on `github.com/temporalio/api-go`; it adds
`update.v1.Request.RequestId` and `.CompletionCallbacks`, which `nexus_worker`'s
`handler/handler.go` uses (`buildCompletionCallbacks`) for the nexus-workflow-update
streaming feature (backed by the custom `Quinn-With-Two-Ns/temporal` server fork referenced
in the Makefile). No public api release carries those fields.

`lambdaworker@v0.1.1` floors `go.temporal.io/api` at **v1.62.5**. The pinned pseudo-version
sorts *below* v1.62.5 (a `-0.` pre-release of v1.62.3, ≈ v1.62.2), so under a Go workspace's
single-version resolution (MVS across all modules), adding lambdaworker drags the whole
workspace up to public v1.62.5 — which drops the `update-callback` fields and breaks
`nexus_worker` **in workspace mode**.

Scope of the break: **workspace mode only** (gopls, and `go build ./...` run inside a module
without `GOWORK=off`). This repo builds **per-module** (each module's `Makefile` runs
`go build ./...`; there are no CI workflows or Dockerfiles), and per-module builds are
unaffected — each module resolves its own go.mod.

**Interim resolution (superseded):** a temporary `replace go.temporal.io/api => …<pinned>`
in `nexus/go.work` forced the whole workspace onto the pinned `update-callback` build. This
was the correct fix while the fields lived only on the unmerged branch.

**Final resolution (2026-07-06, verified):** the `update-callback` feature merged upstream
and shipped publicly — `update.v1.Request.RequestId`/`CompletionCallbacks` are present in
**public api v1.62.13+** (field decls byte-identical to the pinned build). PR #24 un-forked
the dev server to `temporalio/temporal@main`. So all three modules were moved off the custom
pseudo-version onto **public api v1.62.14**, and the `go.work` replace was removed. api
v1.62.14 requires `go 1.25.4`, so the module and `go.work` `go` directives were bumped to
1.25.4. lambdaworker's v1.62.5 floor is satisfied by v1.62.14.

Verified: all three modules build + vet + test in both workspace and per-module (`GOWORK=off`)
modes; the standalone Lambda artifact cross-compiles; and the durability integration suite
(`nexus/Makefile` `test-integration`, embedded dev server) passes — confirming the
completion-callback wire protocol works against the mainline server with the public api.

## Architecture

### 1. Shared registration package — `slack_connector/connectorworker/register.go`

Both `worker.Worker` and `lambdaworker.Options` satisfy `worker.Registry`. Extract the
bot → driver → register chain (currently inlined in `cmd/worker/main.go`) into one function
both binaries call:

```go
package connectorworker

// Register builds the Slack driver and registers the connector workflow and its
// activities onto r. It works for both a standard worker.Worker and a
// lambdaworker.Options, since both implement worker.Registry.
func Register(r worker.Registry, slackBotToken string) error {
    bot, err := slack.NewSlackBot(slackBotToken)
    if err != nil {
        return fmt.Errorf("initialise Slack bot: %w", err)
    }
    if bot.UserID != "" {
        log.Printf("Bot user ID: %s", bot.UserID)
    }
    driver := slack.NewSlackPlatform(bot.Client, bot.TeamID)
    c := connector.NewConnectorWorkflow(&agentiface.TemporalNativeHarnessDriver{})
    r.RegisterWorkflowWithOptions(c.Run, workflow.RegisterOptions{Name: connector.WorkflowName})
    r.RegisterActivityWithOptions(driver.Stream, activity.RegisterOptions{Name: msgiface.StreamActivity})
    r.RegisterActivityWithOptions(driver.PostMessage, activity.RegisterOptions{Name: msgiface.PostMessageActivity})
    r.RegisterActivityWithOptions(driver.PostApprovalPrompt, activity.RegisterOptions{Name: msgiface.PostApprovalPromptActivity})
    return nil
}
```

### 2. `cmd/worker/main.go` — behavior unchanged

Replace the inline bot/driver/registration block (lines ~59–73 today) with a single
`connectorworker.Register(w, flags.slackBotToken)` call. Everything else stays: `client.Dial`
with `TEMPORAL_ADDRESS` / `CONNECTOR_NAMESPACE`, `worker.New`, `w.Run(worker.InterruptCh())`.
No AWS dependencies enter this binary's import graph.

### 3. `cmd/worker-lambda/main.go` — new binary

```go
func main() {
    lambdaworker.RunWorker(worker.WorkerDeploymentVersion{
        DeploymentName: getenvOr("WORKER_DEPLOYMENT_NAME", "nexus-connector-slack"),
        BuildID:        getenvOr("WORKER_BUILD_ID", "dev"), // CI sets to the git SHA
    }, func(o *lambdaworker.Options) error {
        o.WorkerOptions.DeploymentOptions.DefaultVersioningBehavior = workflow.VersioningBehaviorPinned
        if o.TaskQueue == "" { // TEMPORAL_TASK_QUEUE, if set, already populated it
            o.TaskQueue = "nexus-connector-slack"
        }
        if o.ClientOptions.Namespace == "" { // parity with the existing worker's default
            o.ClientOptions.Namespace = "connector"
        }
        return connectorworker.Register(o, os.Getenv("SLACK_BOT_TOKEN"))
    })
}
```

Notes:
- `SLACK_BOT_TOKEN` is still required (surfaced as a `configure` error → `os.Exit(1)` via
  `RunWorker`, or checked explicitly before the call for a clearer message). In production
  it comes from Secrets Manager wired to the Lambda env.
- Namespace and task-queue defaults mirror today's worker so behavior is consistent unless
  explicitly overridden by `TEMPORAL_NAMESPACE` / `TEMPORAL_TASK_QUEUE`.

### 4. Dependencies

- Add `go.temporal.io/sdk/contrib/aws/lambdaworker v0.1.1` to `slack_connector/go.mod`
  (brings `aws-lambda-go`, `contrib/envconfig`; floors `go.temporal.io/api` at v1.62.5).
- Keep `go.temporal.io/sdk` at `v1.41.1` (SDK upgrade abandoned — see the dependency-conflict
  section above).
- Unify all three modules on public `go.temporal.io/api v1.62.14` (see the final resolution
  above); no `go.work` replace is needed. Bump module + `go.work` `go` directives to 1.25.4
  (required by api v1.62.14).
- `go mod tidy` (each module) then confirm `go build`/`go vet`/`go test` pass across the
  workspace **and** that each module still builds per-module (`GOWORK=off`).

## Deployment (added 2026-07-06)

Deployment targets **Temporal Cloud Serverless Workers**: Temporal Cloud invokes the Lambda
when tasks arrive (assuming an IAM role in the account, guarded by an external ID) — there is
no EventBridge/cron. Artifacts live under `nexus/slack_connector/deploy/` with the runbook in
`deploy/README-lambda.md`:

- **App:** `cmd/worker-lambda/secrets.go` fetches the Temporal Cloud API key and Slack bot
  token from Secrets Manager (plain-string secrets) when `TEMPORAL_API_KEY_SECRET_ARN` /
  `SLACK_BOT_TOKEN_SECRET_ARN` are set — the API key becomes TLS-enabled API-key credentials;
  both fall back to env vars for local/dev.
- **Build:** `make build-lambda` produces an arm64 `bootstrap` zip (`provided.al2023`). The
  arm64 choice is a single source of truth: `LAMBDA_ARCH` in the Makefile must match
  `Architectures` in `deploy/worker-lambda.cfn.yaml`.
- **IaC:** `deploy/worker-lambda.cfn.yaml` (function + execution role scoped to
  `secretsmanager:GetSecretValue` on the two ARNs + log group) and
  `deploy/temporal-cloud-serverless-worker-role.yaml` (Temporal's official invocation role,
  copied verbatim; external ID defaulted to `tmprl-<uuid>`). Both pass `cfn-lint`.
- **Registration:** `temporal worker deployment create` / `create-version --aws-lambda-*` /
  `set-current-version`. The `WORKER_BUILD_ID` (git SHA), `WORKER_DEPLOYMENT_NAME`, task
  queue, and external ID form a contract that must match on both the AWS and Cloud sides.
- **Pinned versioning requires server-side ramp:** each new `BuildID` must be set current or
  tasks won't route to it.
- **Verification boundary:** verified through compile, arm64 package, and `cfn-lint`; the
  runtime path (Cloud invocation, assume-role, secret fetch, API-key TLS) needs a real deploy.

## Testing

- `go build ./...` and `go vet ./...` must pass after the dependency changes (this is the
  primary guard: both binaries compile against the `worker.Registry` contract, so a
  registration signature drift fails the build).
- Existing `connector` package workflow tests continue to cover workflow behavior; the shared
  `Register` function is thin glue and its correctness is enforced by the shared compile-time
  `worker.Registry` interface.
- No new network-dependent tests around `slack.NewSlackBot` (the existing `cmd/worker` has no
  unit tests; we match that rather than introduce a live Slack dependency in CI).

## Out of scope

- ~~Infrastructure-as-code for the Lambda + Secrets Manager wiring.~~ Added 2026-07-06 (see
  the Deployment section). Still out: creating the Secrets Manager secrets and the S3 artifact
  bucket themselves, and any CI pipeline automating build → upload → deploy → set-current.
- OpenTelemetry / ADOT observability (the `lambdaworker/otel` sub-package) — can be added
  later via `Options.OnShutdown` + client options.
- Making the `cmd/webhook` HTTP receiver serverless (different mechanism; not requested).
