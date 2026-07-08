# Deploying the Slack webhook to AWS Lambda + API Gateway

Runbook for `cmd/webhook-lambda`: the Slack webhook receiver as a Go Lambda fronted by an
**API Gateway HTTP API**. Slack posts signed requests to the API URL; the Lambda verifies the
signature (signing secret) and starts connector workflows via the Temporal client. Unlike the
worker, this is an ordinary request/response Lambda — API Gateway invokes it, not Temporal.

## How API Gateway fits (quick tour)

- **HTTP API** — the public HTTPS front door (cheaper/simpler than REST API).
- **Integration** — an `AWS_PROXY` link that hands the whole request to the Lambda and returns
  its response verbatim (path, headers, body preserved).
- **Route** — we use a catch-all `$default` route → the one integration; the handler's
  `ServeMux` does the `/slack/events` vs `/interactions` vs `/commands` routing internally.
- **Stage** — `$default` with auto-deploy, so the URL has **no stage prefix** and the paths
  line up: `https://<api-id>.execute-api.<region>.amazonaws.com/slack/events`.
- **Invoke permission** — a resource policy letting API Gateway call the function.

All five are created by `webhook-lambda.cfn.yaml`.

## Prerequisites

- Three **plain-string** Secrets Manager secrets: Temporal Cloud **API key**, Slack **signing
  secret** (Slack app → Basic Information), Slack **bot token**. Note the ARNs.
- If those secrets use a **customer-managed KMS key**, pass its ARN as `SecretsKmsKeyArn`
  (adds `kms:Decrypt`); otherwise leave it empty.
- An **S3 bucket** for the artifact; `aws` CLI; a Temporal Cloud namespace.
- The connector **worker** (`cmd/worker-lambda` or `cmd/worker`) and the **agent** side must be
  running for the workflows this starts to actually execute.

```bash
export AWS_REGION=us-west-2
export ARTIFACT_BUCKET=my-lambda-artifacts
export SHA=$(git rev-parse --short HEAD)
export NAMESPACE=connector.<account-id>
export API_KEY_SECRET_ARN=arn:aws:secretsmanager:...:temporal-api-key
export SIGNING_SECRET_ARN=arn:aws:secretsmanager:...:slack-signing-secret
export SLACK_SECRET_ARN=arn:aws:secretsmanager:...:slack-bot-token
```

## 1. Build & upload

```bash
cd nexus/slack_connector
make build-webhook-lambda            # -> build/webhook-lambda/webhook-lambda.zip (arm64)
aws s3 cp build/webhook-lambda/webhook-lambda.zip \
  "s3://$ARTIFACT_BUCKET/webhook-lambda/$SHA.zip" --region "$AWS_REGION"
```

## 2. Deploy the stack

```bash
aws cloudformation deploy \
  --stack-name slack-connector-webhook-lambda \
  --template-file deploy/webhook-lambda.cfn.yaml \
  --capabilities CAPABILITY_IAM \
  --region "$AWS_REGION" \
  --parameter-overrides \
    ArtifactBucket="$ARTIFACT_BUCKET" \
    ArtifactKey="webhook-lambda/$SHA.zip" \
    TemporalAddress="$AWS_REGION.aws.api.temporal.io:7233" \
    TemporalNamespace="$NAMESPACE" \
    TemporalApiKeySecretArn="$API_KEY_SECRET_ARN" \
    SlackSigningSecretArn="$SIGNING_SECRET_ARN" \
    SlackBotTokenSecretArn="$SLACK_SECRET_ARN"
    # Optional: add SecretsKmsKeyArn=<cmk-arn> if the secrets use a customer-managed KMS key.
    # Cold-start tip: pass BotUserId=U0123ABC instead of SlackBotTokenSecretArn to skip the
    # Slack auth.test call at cold start (which counts against Slack's 3s response budget).
```

Grab the Slack-facing URLs:

```bash
aws cloudformation describe-stacks --stack-name slack-connector-webhook-lambda \
  --region "$AWS_REGION" --query 'Stacks[0].Outputs' --output table
```

## 3. Point your Slack app at the URLs

In the Slack app config (api.slack.com/apps → your app), set the request URLs to the stack
outputs:

- **Event Subscriptions** → Request URL → `…/slack/events`. Slack immediately sends a signed
  `url_verification` challenge; the handler echoes it back, so the URL verifies only once the
  function + signing secret are live.
- **Interactivity & Shortcuts** → Request URL → `…/slack/interactions`.
- **Slash Commands** → each command's Request URL → `…/slack/commands`.

The signing secret in Secrets Manager must match the one shown on the app's **Basic
Information** page, or every request is rejected with 401.

## 4. Verify

- Slack's "Verified ✓" on the Event Subscriptions URL confirms reachability + signature.
- Mention the bot in a channel (or run a slash command) and confirm a connector workflow
  starts in the Temporal UI.
- Check CloudWatch Logs under `/aws/lambda/nexus-connector-slack-webhook`; a rejected request
  logs `signature verification failed` and returns 401.

## Subsequent deploys

New code → new `$SHA` → rebuild/upload → `cloudformation deploy` with the new `ArtifactKey`.
The API URL is stable across deploys, so you don't re-configure Slack.

## Things to watch on the first real deploy

- **Raw-body integrity is the #1 thing to confirm.** Slack's HMAC is over the exact request
  bytes. Between Slack and the handler sit API Gateway (which may set `isBase64Encoded`) and the
  `httpadapter` v2 adapter (which decodes it back). The adapter handles this correctly, but if
  anything transforms the bytes, **every request 401s even with the right secret**. Slack's
  "Verified ✓" on the Events URL is exactly the signal that the body + signature survived the
  round trip — treat it as the first checkpoint.
- **Cold start vs Slack's 3s timeout.** Cold start stacks secret fetches + the Temporal Cloud
  TLS dial (+ `auth.test` if `BotUserId` is unset) before the first response. If it drifts past
  3s, Slack retries and can disable event delivery. Set `BotUserId` to drop the `auth.test`
  call; use provisioned concurrency if cold starts remain a problem.

## Note: the standalone `cmd/webhook` also verifies signatures now

Signature verification lives in the shared handler (`messaging/slack/webhook/server.go`), so the
non-Lambda `cmd/webhook` binary now enforces it too. Any client hitting it without a valid Slack
signature (local `curl`, a plain health check) gets 401 — intended parity, but worth knowing.

## Verification boundary

Verified here through compile, arm64 packaging, and `cfn-lint`. The runtime path — API Gateway
invocation, the signature check against a real Slack request, secret fetch under the execution
role, and starting workflows against your namespace — needs an actual deploy to confirm.
