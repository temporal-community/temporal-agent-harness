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

**Resolution (verified):** a single `replace` in `nexus/go.work` forces the whole workspace
onto the pinned `update-callback` build:

```
replace go.temporal.io/api => go.temporal.io/api v1.62.3-0.20260330144107-1e2b1facde20
```

All three modules build + vet + test cleanly in workspace mode with this replace, and
sibling modules keep building per-module. lambdaworker/envconfig/SDK v1.41.1 reference
nothing added between the pinned commit and v1.62.5, so pinning down is safe.
`slack_connector/go.mod` still *requires* api v1.62.5 (lambdaworker's floor) — correct, since
building slack_connector in isolation (e.g. a Lambda CI/Docker build without `go.work`)
compiles fine against public v1.62.5, which it never uses the extra fields from. The replace
governs local/workspace resolution only.

**Caveat:** do not run `go work sync` — it rewrites the sibling go.mod `require` lines to
v1.62.5 (the pre-replace MVS pick) and breaks their per-module builds. A comment in
`go.work` records this.

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
- Add the `go.temporal.io/api` `replace` to `nexus/go.work` (see above).
- `go mod tidy` (slack_connector) then confirm `go build`/`go vet`/`go test` pass across the
  workspace **and** that sibling modules still build per-module (`GOWORK=off`).

## Operational notes (documented, not enforced in code)

These belong in the README / deployment docs for the new binary:

- **Polling is invocation-scoped.** A Lambda worker only polls for tasks while an invocation
  is running, then drains before the deadline. To behave like an always-on worker it must be
  invoked on a tight recurring schedule (e.g. an EventBridge rule every ~1 minute), with the
  function timeout set to at least the longest activity StartToClose plus the shutdown buffer
  (~7s); a 60s minimum timeout is recommended. Temporal's `auto-scaled-workers` project is
  the more sophisticated, backlog-driven way to drive invocations and is out of scope here.
- **Pinned versioning requires server-side ramp.** For each new `BuildID`, the deployment's
  current version must be set on the server (e.g.
  `temporal worker deployment set-current-version`) or tasks will not route to the new build.
- **Build & package:** `GOOS=linux GOARCH=arm64 go build -o bootstrap ./cmd/worker-lambda`,
  zip the `bootstrap` binary, deploy on the `provided.al2023` runtime. Optionally ship a
  `temporal.toml`; env vars are the recommended default with secrets from Secrets Manager.

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

- Infrastructure-as-code (Terraform/CloudFormation) for the Lambda, EventBridge trigger, or
  Secrets Manager wiring.
- OpenTelemetry / ADOT observability (the `lambdaworker/otel` sub-package) — can be added
  later via `Options.OnShutdown` + client options.
- Making the `cmd/webhook` HTTP receiver serverless (different mechanism; not requested).
