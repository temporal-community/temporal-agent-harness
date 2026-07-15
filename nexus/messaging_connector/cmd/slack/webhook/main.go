package main

import (
	"log"
	"net/http"
	"os"

	slackmsg "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging/slack"
	slackwebhook "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging/slack/webhook"
	"go.temporal.io/sdk/client"
)

type flags struct {
	slackBotToken      string
	slackSigningSecret string
	temporalAddress    string
	connectorNamespace string
	taskQueue          string
	webhookPort        string
}

func ensureFlags() *flags {
	slackBotToken := os.Getenv("SLACK_BOT_TOKEN")
	if slackBotToken == "" {
		log.Fatal("SLACK_BOT_TOKEN is required")
	}
	slackSigningSecret := os.Getenv("SLACK_SIGNING_SECRET")
	if slackSigningSecret == "" {
		log.Fatal("SLACK_SIGNING_SECRET is required")
	}

	temporalAddress := os.Getenv("TEMPORAL_ADDRESS")
	if temporalAddress == "" {
		temporalAddress = "localhost:7233"
	}
	connectorNamespace := os.Getenv("CONNECTOR_NAMESPACE")
	if connectorNamespace == "" {
		connectorNamespace = "connector"
	}
	taskQueue := os.Getenv("CONNECTOR_TASK_QUEUE")
	if taskQueue == "" {
		taskQueue = "nexus-connector-slack"
	}
	webhookPort := os.Getenv("WEBHOOK_PORT")
	if webhookPort == "" {
		webhookPort = "8080"
	}
	return &flags{
		slackBotToken:      slackBotToken,
		slackSigningSecret: slackSigningSecret,
		temporalAddress:    temporalAddress,
		connectorNamespace: connectorNamespace,
		taskQueue:          taskQueue,
		webhookPort:        webhookPort,
	}
}

func main() {
	flags := ensureFlags()

	tc, err := client.Dial(client.Options{
		HostPort:  flags.temporalAddress,
		Namespace: flags.connectorNamespace,
	})
	if err != nil {
		log.Fatalf("Failed to connect to Temporal: %v", err)
	}
	defer tc.Close()

	bot, err := slackmsg.NewSlackBot(flags.slackBotToken)
	if err != nil {
		log.Fatalf("Failed to initialise Slack bot: %v", err)
	}
	if bot.UserID != "" {
		log.Printf("Slack bot user ID: %s (forwarding only messages that mention the bot)", bot.UserID)
	}

	handler := slackwebhook.NewServer(tc, flags.taskQueue, flags.slackSigningSecret, bot.UserID)
	addr := ":" + flags.webhookPort
	log.Printf("Slack webhook server listening on %s", addr)
	if err := http.ListenAndServe(addr, handler); err != nil {
		log.Fatalf("Webhook server error: %v", err)
	}
}
