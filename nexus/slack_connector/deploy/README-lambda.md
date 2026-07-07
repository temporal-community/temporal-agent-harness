# Deploying the Slack connector worker to AWS Lambda (Temporal Cloud Serverless Workers)

End-to-end runbook for `cmd/worker-lambda`. The model: **Temporal Cloud invokes the
Lambda** when tasks arrive (it assumes an IAM role in your account, guarded by an external
ID) — there is no EventBridge/cron to set up. See `../cmd/worker-lambda/README.md` for the
binary's runtime behavior and config.

> Temporal Cloud Serverless Workers is a **pre-release** feature; you may need to request
> access via a support ticket / your account team. Commands below are the current documented
> shape — treat them as a guide and cross-check against Temporal's docs for your account.

## What's in this directory

| File | Purpose |
|---|---|
| `worker-lambda.cfn.yaml` | The Lambda function + execution role (logs, `GetSecretValue`) + log group. |
| `temporal-cloud-serverless-worker-role.yaml` | Temporal's official invocation role (Cloud assumes this to invoke the function). Copied verbatim; external ID defaulted. |

## Prerequisites

- Two **plain-string** Secrets Manager secrets: the Temporal Cloud **API key** and the
  **Slack bot token**. Note their ARNs.
- An **S3 bucket** for the deployment artifact.
- `aws` CLI + `temporal` CLI, and a Temporal Cloud namespace.
- Go toolchain (for `make build-lambda`).
- The **agent Nexus worker** must also be running and its Nexus endpoint registered in the
  same namespace — this connector calls the agent over Nexus (`AgentNexusEndpoint`), so the
  Lambda alone won't make Slack work end-to-end.

Set shared variables (one git SHA ties the artifact, the function's `WORKER_BUILD_ID`, and
the Temporal Cloud deployment version together):

```bash
export AWS_REGION=us-west-2
export ARTIFACT_BUCKET=my-lambda-artifacts
export SHA=$(git rev-parse --short HEAD)
export NAMESPACE=connector.<account-id>
export API_KEY_SECRET_ARN=arn:aws:secretsmanager:...:temporal-api-key
export SLACK_SECRET_ARN=arn:aws:secretsmanager:...:slack-bot-token
```

## 1. Build & upload the artifact

```bash
cd nexus/slack_connector
make build-lambda                                   # -> build/worker-lambda/worker-lambda.zip (arm64)
aws s3 cp build/worker-lambda/worker-lambda.zip \
  "s3://$ARTIFACT_BUCKET/worker-lambda/$SHA.zip" --region "$AWS_REGION"
```

Keying the S3 object by `$SHA` matters: CloudFormation only redeploys code when the S3 key
(or object version) changes.

## 2. Deploy the function stack

```bash
aws cloudformation deploy \
  --stack-name slack-connector-worker-lambda \
  --template-file deploy/worker-lambda.cfn.yaml \
  --capabilities CAPABILITY_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides \
    ArtifactBucket="$ARTIFACT_BUCKET" \
    ArtifactKey="worker-lambda/$SHA.zip" \
    TemporalAddress="$AWS_REGION.aws.api.temporal.io:7233" \
    TemporalNamespace="$NAMESPACE" \
    WorkerBuildId="$SHA" \
    TemporalApiKeySecretArn="$API_KEY_SECRET_ARN" \
    SlackBotTokenSecretArn="$SLACK_SECRET_ARN"

# Grab the function ARN for the next steps:
export FUNCTION_ARN=$(aws cloudformation describe-stacks \
  --stack-name slack-connector-worker-lambda --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`FunctionArn`].OutputValue' --output text)
```

## 3. Deploy the invocation-role stack

Grant Temporal Cloud permission to invoke the function. The external ID defaults to the
value baked into `temporal-cloud-serverless-worker-role.yaml`; override it if you rotate it.
Use the function ARN with a `:*` suffix so all published versions are covered.

```bash
aws cloudformation deploy \
  --stack-name temporal-cloud-invoke-role \
  --template-file deploy/temporal-cloud-serverless-worker-role.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides "LambdaFunctionARNs=$FUNCTION_ARN:*"

export ROLE_ARN=$(aws cloudformation describe-stacks \
  --stack-name temporal-cloud-invoke-role --region "$AWS_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`RoleARN`].OutputValue' --output text)
export EXTERNAL_ID=''   # must match the template
```

## 4. Register with Temporal Cloud

The `WORKER_DEPLOYMENT_NAME` env var on the function (default `nexus-connector-slack`) must
equal the deployment `--name`, and `WORKER_BUILD_ID` (=`$SHA`) must equal `--build-id`.
Versioning is **Pinned**, so tasks route only to the current version you set.

```bash
temporal worker deployment create \
  --namespace "$NAMESPACE" --name nexus-connector-slack

temporal worker deployment create-version \
  --namespace "$NAMESPACE" \
  --deployment-name nexus-connector-slack \
  --build-id "$SHA" \
  --aws-lambda-function-arn "$FUNCTION_ARN" \
  --aws-lambda-assume-role-arn "$ROLE_ARN" \
  --aws-lambda-assume-role-external-id "$EXTERNAL_ID"

temporal worker deployment set-current-version \
  --namespace "$NAMESPACE" \
  --deployment-name nexus-connector-slack --build-id "$SHA"
```

(Or do the same via the Cloud UI: **Workers → Create Worker Deployment → AWS Lambda**, which
sets the version current automatically.)

## 5. Verify

- Trigger a Slack interaction (or start a test workflow on the task queue) and watch the
  workflow event history in the Temporal UI show task completions.
- Check CloudWatch Logs under `/aws/lambda/nexus-connector-slack-worker` for invocation +
  graceful-shutdown lines.

## Subsequent deploys

New code → new `$SHA` → rebuild/upload → `cloudformation deploy` the function stack with the
new `ArtifactKey`/`WorkerBuildId` → `create-version` + `set-current-version` for the new
`--build-id`. Because it's Pinned, running workflows stay on their version until you migrate;
new workflows use the current version.

## Verification boundary

Everything in this repo is verified up to **compile, package (arm64), and `cfn-lint`**. The
runtime path — Temporal Cloud invoking the function, the assume-role handshake, the
`GetSecretValue` call under the execution role, and the API-key TLS handshake — can only be
confirmed by an actual deploy against your account and namespace.
