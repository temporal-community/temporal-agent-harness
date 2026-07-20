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

	slackmsg "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound/driver/slack"
	slackwebhook "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound/driver/slack/webhook"

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

	// The bot user ID is used only to filter for messages that mention the bot.
	// Prefer BOT_USER_ID (set on the function) to avoid an auth.test round-trip on
	// every cold start — that call counts against Slack's 3s response budget.
	// Fall back to deriving it from the bot token when BOT_USER_ID is unset.
	botUserID := os.Getenv("BOT_USER_ID")
	if botUserID == "" {
		if botToken == "" {
			log.Fatal("provide BOT_USER_ID, or a bot token via SLACK_BOT_TOKEN_SECRET_ARN / SLACK_BOT_TOKEN")
		}
		bot, err := slackmsg.NewSlackBot(botToken)
		if err != nil {
			log.Fatalf("Failed to initialise Slack bot: %v", err)
		}
		botUserID = bot.UserID
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

	taskQueue := getenvOr("TEMPORAL_TASK_QUEUE", "nexus-connector-slack")
	// Must match the adapter's AGENT_WORKFLOW_ID_PREFIX so the webhook can find a
	// thread's existing agent session (for mention-free in-thread continuation).
	agentWFPrefix := getenvOr("AGENT_WORKFLOW_ID_PREFIX", "agent-")
	server := slackwebhook.NewServer(tc, taskQueue, signingSecret, botUserID, agentWFPrefix)

	// Adapt the net/http handler to API Gateway HTTP API (payload v2) events.
	lambda.Start(httpadapter.NewV2(server).ProxyWithContext)
}
