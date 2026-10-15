package main

import (
	"log"
	"net/http"
	"os"
	"strings"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack/inbound"
	"go.temporal.io/sdk/client"
)

type flags struct {
	slackBotToken       string
	slackSigningSecret  string
	temporalAddress     string
	connectorNamespace  string
	taskQueue           string
	identity            string
	webhookPort         string
	allowedBotIDs       []string
	guideOnFirstContact bool
}

// splitCommaList splits a comma-separated env var into trimmed, non-empty
// values. Returns nil for an empty or blank input.
func splitCommaList(s string) []string {
	var out []string
	for _, part := range strings.Split(s, ",") {
		if part = strings.TrimSpace(part); part != "" {
			out = append(out, part)
		}
	}
	return out
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
	identity := os.Getenv("CONNECTOR_IDENTITY")
	webhookPort := os.Getenv("WEBHOOK_PORT")
	if webhookPort == "" {
		webhookPort = "8080"
	}
	guideOnFirstContact := os.Getenv("SLACK_GUIDE_ON_FIRST_CONTACT") != "false"
	allowedBotIDs := splitCommaList(os.Getenv("ALLOWED_INBOUND_BOT_IDS"))
	return &flags{
		slackBotToken:       slackBotToken,
		slackSigningSecret:  slackSigningSecret,
		temporalAddress:     temporalAddress,
		connectorNamespace:  connectorNamespace,
		taskQueue:           taskQueue,
		identity:            identity,
		webhookPort:         webhookPort,
		allowedBotIDs:       allowedBotIDs,
		guideOnFirstContact: guideOnFirstContact,
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

	bot, err := slack.NewSlackBot(flags.slackBotToken)
	if err != nil {
		log.Fatalf("Failed to initialise Slack bot: %v", err)
	}
	if bot.UserID != "" {
		log.Printf("Slack bot user ID: %s (forwarding mentions, plus replies in threads the bot was mentioned in)", bot.UserID)
	}

	handler := slackinbound.NewServer(tc, flags.taskQueue, flags.slackSigningSecret, slackinbound.Options{
		Identity:            flags.identity,
		BotUserID:           bot.UserID,
		AllowedBotIDs:       flags.allowedBotIDs,
		GuideOnFirstContact: flags.guideOnFirstContact,
		Slack:               bot.Client,
	})
	addr := ":" + flags.webhookPort
	log.Printf("Slack webhook server listening on %s", addr)
	if err := http.ListenAndServe(addr, handler); err != nil {
		log.Fatalf("Webhook server error: %v", err)
	}
}
