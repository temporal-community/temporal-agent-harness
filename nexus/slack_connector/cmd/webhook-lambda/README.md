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
| `TEMPORAL_API_KEY_SECRET_ARN` | Secret ARN (plain string) for the Temporal Cloud API key (→ TLS API-key credentials). | — |
| `SLACK_SECRETS_ARN` | Secret ARN for a JSON secret; the `SLACK_SIGNING_SECRET` (request verification) and `SLACK_BOT_TOKEN` (bot user ID) fields are read from it. | — |
| `TEMPORAL_API_KEY` / `SLACK_SIGNING_SECRET` / `SLACK_BOT_TOKEN` | Plain-value fallbacks used when the ARN vars are unset (local/dev). | — |
| `BOT_USER_ID` | Slack bot user ID. If set, the bot token is not needed and the cold-start `auth.test` call is skipped. | — |
| `TEMPORAL_ADDRESS` | Temporal frontend address | `localhost:7233` |
| `TEMPORAL_NAMESPACE` | Namespace | `connector` |
| `TEMPORAL_TASK_QUEUE` | Task queue connector workflows start on | `nexus-connector-slack` |

The signing secret is required, and so is the bot user ID — supply it directly via
`BOT_USER_ID`, or let it be derived from the bot token (the `SLACK_BOT_TOKEN` field
of `SLACK_SECRETS_ARN`, or the `SLACK_BOT_TOKEN` env var). The process exits at
startup if neither is available. The API key may be empty for a local dev server
with no auth.

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
