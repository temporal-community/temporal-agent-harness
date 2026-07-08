// Command webhook-lambda runs the Slack webhook receiver as an AWS Lambda behind
// an API Gateway HTTP API. It wraps the same net/http handler used by cmd/webhook
// (via the aws-lambda-go-api-proxy v2 adapter), so routing and signature
// verification are identical to the standalone server.
//
// Secrets (Temporal Cloud API key, Slack signing secret, Slack bot token) are
// fetched from Secrets Manager at cold start when their *_ARN env vars are set,
// falling back to plain env vars for local/dev. The Temporal client and handler
// are built once per cold start and reused across invocations.
//
// See ../deploy/README-webhook-lambda.md for the deployment runbook.
package main

import (
	"context"
	"crypto/tls"
	"log"
	"os"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/awslabs/aws-lambda-go-api-proxy/httpadapter"

	slackmsg "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging/slack"
	slackwebhook "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging/slack/webhook"

	"go.temporal.io/sdk/client"
)

func getenvOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	apiKey, signingSecret, botToken, err := resolveSecrets(ctx)
	if err != nil {
		log.Fatalf("Failed to resolve secrets: %v", err)
	}
	if signingSecret == "" {
		log.Fatal("Slack signing secret is required (SLACK_SIGNING_SECRET_ARN or SLACK_SIGNING_SECRET)")
	}
	if botToken == "" {
		log.Fatal("Slack bot token is required (SLACK_BOT_TOKEN_SECRET_ARN or SLACK_BOT_TOKEN)")
	}

	opts := client.Options{
		HostPort:  getenvOr("TEMPORAL_ADDRESS", "localhost:7233"),
		Namespace: getenvOr("TEMPORAL_NAMESPACE", "connector"),
	}
	if apiKey != "" {
		// Temporal Cloud API-key auth requires TLS.
		opts.Credentials = client.NewAPIKeyStaticCredentials(apiKey)
		opts.ConnectionOptions.TLS = &tls.Config{}
	}
	tc, err := client.Dial(opts)
	if err != nil {
		log.Fatalf("Failed to connect to Temporal: %v", err)
	}
	defer tc.Close()

	bot, err := slackmsg.NewSlackBot(botToken)
	if err != nil {
		log.Fatalf("Failed to initialise Slack bot: %v", err)
	}

	taskQueue := getenvOr("TEMPORAL_TASK_QUEUE", "nexus-connector-slack")
	server := slackwebhook.NewServer(tc, taskQueue, signingSecret, bot.UserID)

	// Adapt the net/http handler to API Gateway HTTP API (payload v2) events.
	lambda.Start(httpadapter.NewV2(server).ProxyWithContext)
}
