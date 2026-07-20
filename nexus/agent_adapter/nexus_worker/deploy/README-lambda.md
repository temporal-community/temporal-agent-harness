# Deploying the agent-adapter Nexus worker to AWS Lambda (Temporal Cloud Serverless Workers)

Runbook for `cmd/worker-lambda`: the `AgentService` Nexus handler as a Nexus-only Go Lambda.
Temporal Cloud invokes it when Nexus tasks arrive on its task queue — the task queue the
`support-agent-nexus` targets. The connector workflow calls the agent over that endpoint; this
worker translates those Nexus operations into workflow updates/queries against the **Python**
agent workflow. See `../cmd/worker-lambda/README.md` for runtime behavior and config.

> Temporal Cloud Serverless Workers is a **pre-release** feature. Crucially, the docs do not
> confirm whether the Worker Controller triggers a Lambda invocation for **Nexus** tasks
> (vs. only workflow/activity tasks). This deploy is the way to find out — watch for a
> CloudWatch invocation in step 6. If none fires, this worker must run on always-on compute
> (ECS/Fargate) via `cmd/main.go` instead; everything else here still applies.

## How this fits with the rest of the system

```
Slack → webhook-lambda → connector workflow ──Nexus(support-agent-nexus)──▶ THIS worker
                                                                               │ update/query
                                                                               ▼
                                                                     Python agent workflow
                                                                        (AGENT_TASK_QUEUE)
```

The endpoint routes to **this worker's namespace + task queue**. So three values must line up:
- `TEMPORAL_NAMESPACE` (this worker) == the endpoint's `--target-namespace`
- `TEMPORAL_TASK_QUEUE` (this worker, default `nexus-agent-go`) == the endpoint's `--target-task-queue`
- `AGENT_TASK_QUEUE` (default `agent`) == where the Python agent workflow actually runs
- `AGENT_WORKFLOW_NAME` == the Python agent's registered workflow type name

## Prerequisites

- A **plain-string** Secrets Manager secret for the Temporal Cloud **API key**. Note its ARN.
  (No Slack secret is needed on this side.)
- If it uses a **customer-managed KMS key**, pass its ARN as `SecretsKmsKeyArn` (adds `kms:Decrypt`).
- An **S3 bucket** for the artifact; `aws` + `temporal` CLIs; a Temporal Cloud namespace.
- The **Python agent worker** must be running on `AGENT_TASK_QUEUE` for calls to complete —
  this worker only bridges Nexus → workflow update; it doesn't run the agent itself.

```bash
export AWS_REGION=us-east-2
export ARTIFACT_BUCKET=my-lambda-artifacts
export SHA=$(git rev-parse --short HEAD)
export NAMESPACE=<agent-namespace>            # where the agent workflow runs
export API_KEY_SECRET_ARN=arn:aws:secretsmanager:...:temporal-api-key
export AGENT_WORKFLOW_NAME=<AgentWorkflowType>
```

## 1. Build & upload the artifact

```bash
cd nexus/agent_adapter/nexus_worker
make build-lambda                                   # -> build/worker-lambda/worker-lambda.zip (arm64)
aws s3 cp build/worker-lambda/worker-lambda.zip \
  "s3://$ARTIFACT_BUCKET/nexus-agent-worker/$SHA.zip" --region "$AWS_REGION"
```

## 2. Deploy the function stack

```bash
aws cloudformation deploy \
  --stack-name nexus-agent-worker-lambda \
  --template-file deploy/worker-lambda.cfn.yaml \
  --capabilities CAPABILITY_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides \
    ArtifactBucket="$ARTIFACT_BUCKET" \
    ArtifactKey="nexus-agent-worker/$SHA.zip" \
    TemporalAddress="$AWS_REGION.aws.api.temporal.io:7233" \
    TemporalNamespace="$NAMESPACE" \
    WorkerBuildId="$SHA" \
    AgentWorkflowName="$AGENT_WORKFLOW_NAME" \
    TemporalApiKeySecretArn="$API_KEY_SECRET_ARN"
    # Optional: AgentTaskQueue=<queue> (default "agent"), TemporalTaskQueue=<queue> (default "nexus-agent-go").
    # Optional: SecretsKmsKeyArn=<cmk-arn> if the secret uses a customer-managed KMS key.

export FUNCTION_ARN=$(aws cloudformation describe-stacks \
  --stack-name nexus-agent-worker-lambda --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`FunctionArn`].OutputValue' --output text)
```

## 3. Let Temporal Cloud invoke the function

Temporal Cloud assumes an IAM role in your account to invoke the Lambda. You can **reuse the
same invocation role** as the connector worker (recommended) — just add this function's ARN to
its policy `Resource` list — or deploy a dedicated one from
[`../../../ui_connector/slack_to_temporal_agent_harness_connector/deploy/temporal-cloud-serverless-worker-role.yaml`](../../../ui_connector/slack_to_temporal_agent_harness_connector/deploy/temporal-cloud-serverless-worker-role.yaml).

```bash
export ROLE_ARN=<existing invoke role ARN>
export EXTERNAL_ID=<its external ID>
```

Confirm the role's policy allows `lambda:InvokeFunction` on `"$FUNCTION_ARN:*"` (add it if
reusing a role scoped to another function).

## 4. Register with Temporal Cloud (Worker Deployment)

> **Point the `temporal` CLI at Cloud first.** The commands below hit `localhost:7233` by
> default and will silently fail against a Cloud namespace otherwise (the same trap as the
> connector deploy). Configure a cloud env once and pass `--env cloud` to every `temporal`
> command in steps 4–5:
>
> ```bash
> temporal env set cloud --address "$AWS_REGION.aws.api.temporal.io:7233"
> temporal env set cloud --namespace "$NAMESPACE"
> temporal env set cloud --api-key "$(aws secretsmanager get-secret-value \
>   --secret-id "$API_KEY_SECRET_ARN" --region "$AWS_REGION" --query SecretString --output text)"
> temporal env set cloud --tls   # API-key auth requires TLS
> ```

`WORKER_DEPLOYMENT_NAME` (default `nexus-agent-go`) must equal `--name`, and `WORKER_BUILD_ID`
(=`$SHA`) must equal `--build-id`.

```bash
temporal worker deployment create \
  --env cloud --name nexus-agent-go

temporal worker deployment create-version \
  --env cloud \
  --deployment-name nexus-agent-go \
  --build-id "$SHA" \
  --aws-lambda-function-arn "$FUNCTION_ARN" \
  --aws-lambda-assume-role-arn "$ROLE_ARN" \
  --aws-lambda-assume-role-external-id "$EXTERNAL_ID"

temporal worker deployment set-current-version \
  --env cloud \
  --deployment-name nexus-agent-go --build-id "$SHA"
```

## 5. Register the Nexus endpoint

The connector calls `AgentNexusEndpoint = "support-agent-nexus"` (see
`ui_connector/outbound/driver/temporal_agent_harness/driver.go`). Register it to target this worker's
namespace + task queue. **Endpoint names are account-global**, so this must be unique across your
account.

Temporal Cloud manages Nexus endpoints; create it via **the Cloud UI** (**Namespaces → Nexus →
Create Endpoint**), or via CLI if your account supports it (`temporal operator nexus endpoint
create` against the cloud env, or `tcld nexus endpoint create` — confirm which your tenant
exposes):

```bash
temporal operator nexus endpoint create \
  --env cloud \
  --name support-agent-nexus \
  --target-namespace "$NAMESPACE" \
  --target-task-queue nexus-agent-go
```

**Cross-namespace caveat (most likely silent failure).** If the **connector** runs in a
*different* namespace from this agent worker, the endpoint alone won't route — its allowed-caller
/ namespace access policy must explicitly include the connector's namespace, or the connector
keeps failing (`endpoint "support-agent-nexus" not found` / permission denied). If both run in
the same namespace, no extra policy is needed. Confirm which case you're in before testing.

## 6. Verify

- Trigger a Slack interaction so the connector workflow schedules the Nexus operation.
- **Watch CloudWatch Logs under `/aws/lambda/nexus-agent-worker` for an invocation.** This is the
  pre-release checkpoint: an invocation confirms Temporal Cloud triggers serverless invocation for
  Nexus tasks. No invocation (but the endpoint resolves and the connector no longer errors with
  "endpoint not found") means the trigger doesn't cover Nexus yet → run `cmd/main.go` on always-on
  compute instead.
- Confirm the agent workflow starts/receives the turn in the Temporal UI, and that the connector
  streams a reply back to Slack.

## Subsequent deploys

New code → new `$SHA` → rebuild/upload → `cloudformation deploy` the function stack with the new
`ArtifactKey`/`WorkerBuildId` → `create-version` + `set-current-version` for the new `--build-id`.
The endpoint and invoke role are unchanged.

## Verification boundary

Everything in this repo is verified up to **compile, package (arm64), and `cfn-lint`**. The
runtime path — Temporal Cloud invoking the function for a Nexus task, the assume-role handshake,
`GetSecretValue` under the execution role, and the Nexus operation reaching the Python agent
workflow — can only be confirmed by an actual deploy against your account and namespace.
