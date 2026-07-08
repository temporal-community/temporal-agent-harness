# webhook-lambda

Runs the Slack webhook receiver as an AWS Lambda behind an API Gateway HTTP API.
It wraps the same `net/http` handler as `cmd/webhook` (via the
[aws-lambda-go-api-proxy](https://github.com/awslabs/aws-lambda-go-api-proxy) v2
adapter), so routing and Slack **signature verification** are identical to the
standalone server. On each request it verifies the Slack signature, then starts a
connector workflow via the Temporal client and acks Slack.

The Temporal client and handler are built once per cold start and reused across
invocations.

## Configuration

Secrets are read from AWS Secrets Manager when their `*_ARN` vars are set (plain-string
secrets, fetched once at cold start), falling back to plain env vars for local/dev.

| Variable | Purpose | Default |
|---|---|---|
| `TEMPORAL_API_KEY_SECRET_ARN` | Secret ARN for the Temporal Cloud API key (→ TLS API-key credentials). | — |
| `SLACK_SIGNING_SECRET_ARN` | Secret ARN for the Slack signing secret (request verification). | — |
| `SLACK_BOT_TOKEN_SECRET_ARN` | Secret ARN for the Slack bot token (bot user ID). | — |
| `TEMPORAL_API_KEY` / `SLACK_SIGNING_SECRET` / `SLACK_BOT_TOKEN` | Plain-value fallbacks used when the matching `*_ARN` is unset (local/dev). | — |
| `TEMPORAL_ADDRESS` | Temporal frontend address | `localhost:7233` |
| `TEMPORAL_NAMESPACE` | Namespace | `connector` |
| `TEMPORAL_TASK_QUEUE` | Task queue connector workflows start on | `nexus-connector-slack` |

The signing secret and bot token are required (the process exits at startup if
neither the ARN nor the plain var yields a value). The API key may be empty for a
local dev server with no auth.

## Build & deploy

```
make build-webhook-lambda   # -> build/webhook-lambda/webhook-lambda.zip (provided.al2023, arm64)
```

Full AWS deploy (CloudFormation HTTP API + Slack app config) is in
[`../deploy/README-webhook-lambda.md`](../deploy/README-webhook-lambda.md).

## Routes

The handler serves `POST /slack/events`, `/slack/interactions`, and `/slack/commands`.
API Gateway forwards everything via a catch-all `$default` route; the internal
`ServeMux` dispatches by path.
