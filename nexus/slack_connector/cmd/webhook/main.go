package main

import (
	"fmt"
	"log"
	"net/http"
	"os"

	"go.temporal.io/sdk/client"

	slackmsg "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging/slack"
	slackwebhook "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging/slack/webhook"
	"github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging/teams"
	teamswebhook "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging/teams/webhook"
)

const (
	platformSlack = "slack"
	platformTeams = "teams"
)

type flags struct {
	slackBotToken      string
	slackSigningSecret string
	microsoftAppID     string
	microsoftAppPass   string
	teamsServiceURL    string
	messagingPlatform  string
	temporalAddress    string
	connectorNamespace string
	taskQueue          string
	webhookPort        string
}

func ensureFlags() *flags {
	messagingPlatform := os.Getenv("MESSAGING_PLATFORM")
	if messagingPlatform == "" {
		messagingPlatform = platformSlack
	}

	slackBotToken := os.Getenv("SLACK_BOT_TOKEN")
	slackSigningSecret := os.Getenv("SLACK_SIGNING_SECRET")
	microsoftAppID := os.Getenv("MICROSOFT_APP_ID")
	microsoftAppPass := os.Getenv("MICROSOFT_APP_PASSWORD")
	teamsServiceURL := os.Getenv("TEAMS_SERVICE_URL")

	switch messagingPlatform {
	case platformSlack:
		if slackBotToken == "" {
			log.Fatal("SLACK_BOT_TOKEN is required")
		}
		if slackSigningSecret == "" {
			log.Fatal("SLACK_SIGNING_SECRET is required")
		}
	case platformTeams:
		// Credentials are needed to replace approval cards after a button
		// click via the Bot Connector's Update Activity endpoint.
		if microsoftAppID == "" {
			log.Fatal("MICROSOFT_APP_ID is required")
		}
		if microsoftAppPass == "" {
			log.Fatal("MICROSOFT_APP_PASSWORD is required")
		}
	default:
		log.Fatalf("Unsupported MESSAGING_PLATFORM %q", messagingPlatform)
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
		taskQueue = "nexus-connector-" + messagingPlatform
	}
	webhookPort := os.Getenv("WEBHOOK_PORT")
	if webhookPort == "" {
		webhookPort = "8080"
	}
	return &flags{
		slackBotToken:      slackBotToken,
		slackSigningSecret: slackSigningSecret,
		microsoftAppID:     microsoftAppID,
		microsoftAppPass:   microsoftAppPass,
		teamsServiceURL:    teamsServiceURL,
		messagingPlatform:  messagingPlatform,
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

	handler := newWebhookHandler(tc, flags)
	addr := fmt.Sprintf(":%s", flags.webhookPort)
	log.Printf("%s webhook server listening on %s", flags.messagingPlatform, addr)
	if err := http.ListenAndServe(addr, handler); err != nil {
		log.Fatalf("Webhook server error: %v", err)
	}
}

func newWebhookHandler(tc client.Client, flags *flags) http.Handler {
	switch flags.messagingPlatform {
	case platformSlack:
		bot, err := slackmsg.NewSlackBot(flags.slackBotToken)
		if err != nil {
			log.Fatalf("Failed to initialise Slack bot: %v", err)
		}
		if bot.UserID != "" {
			log.Printf("Slack bot user ID: %s (forwarding only messages that mention the bot)", bot.UserID)
		}
		return slackwebhook.NewServer(tc, flags.taskQueue, flags.slackSigningSecret, bot.UserID)

	case platformTeams:
		bot, err := teams.NewTeamsBot(flags.microsoftAppID, flags.microsoftAppPass)
		if err != nil {
			log.Fatalf("Failed to initialise Teams bot: %v", err)
		}
		log.Printf("Teams bot app ID: %s", bot.AppID)
		platform := teams.NewTeamsPlatform(bot, flags.teamsServiceURL)
		return teamswebhook.NewServer(tc, flags.taskQueue, platform)

	default:
		log.Fatalf("Unsupported MESSAGING_PLATFORM %q", flags.messagingPlatform)
		return nil
	}
}
