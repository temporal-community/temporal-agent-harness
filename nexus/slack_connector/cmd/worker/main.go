package main

import (
	"log"
	"os"

	agentiface "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/agent"
	"github.com/temporalio/temporal-agent-harness/nexus/slack_connector/connector"
	msgiface "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging"
	"github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging/slack"
	"github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging/teams"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

const (
	platformSlack = "slack"
	platformTeams = "teams"
)

type flags struct {
	slackBotToken      string
	microsoftTenantID  string
	microsoftAppID     string
	microsoftAppPass   string
	teamsServiceURL    string
	messagingPlatform  string
	temporalAddress    string
	connectorNamespace string
	taskQueue          string
}

func ensureFlags() *flags {
	messagingPlatform := os.Getenv("MESSAGING_PLATFORM")
	if messagingPlatform == "" {
		messagingPlatform = platformSlack
	}

	slackBotToken := os.Getenv("SLACK_BOT_TOKEN")
	microsoftTenantID := os.Getenv("MICROSOFT_TENANT_ID")
	microsoftAppID := os.Getenv("MICROSOFT_APP_ID")
	microsoftAppPass := os.Getenv("MICROSOFT_APP_PASSWORD")
	teamsServiceURL := os.Getenv("TEAMS_SERVICE_URL")

	switch messagingPlatform {
	case platformSlack:
		if slackBotToken == "" {
			log.Fatal("SLACK_BOT_TOKEN is required")
		}
	case platformTeams:
		if microsoftTenantID == "" {
			log.Fatal("MICROSOFT_TENANT_ID is required")
		}
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
	return &flags{
		slackBotToken:      slackBotToken,
		microsoftTenantID:  microsoftTenantID,
		microsoftAppID:     microsoftAppID,
		microsoftAppPass:   microsoftAppPass,
		teamsServiceURL:    teamsServiceURL,
		messagingPlatform:  messagingPlatform,
		temporalAddress:    temporalAddress,
		connectorNamespace: connectorNamespace,
		taskQueue:          taskQueue,
	}
}

func main() {
	flags := ensureFlags()

	tc, err := client.Dial(client.Options{HostPort: flags.temporalAddress, Namespace: flags.connectorNamespace})
	if err != nil {
		log.Fatalf("Failed to create Temporal client: %v", err)
	}
	defer tc.Close()

	driver := newMessagingPlatform(flags)

	c := connector.NewConnectorWorkflow(&agentiface.TemporalNativeHarnessDriver{})
	w := worker.New(tc, flags.taskQueue, worker.Options{})
	w.RegisterWorkflowWithOptions(c.Run, workflow.RegisterOptions{Name: connector.WorkflowName})
	w.RegisterActivityWithOptions(driver.Stream, activity.RegisterOptions{Name: msgiface.StreamActivity})
	w.RegisterActivityWithOptions(driver.PostMessage, activity.RegisterOptions{Name: msgiface.PostMessageActivity})
	w.RegisterActivityWithOptions(driver.PostApprovalPrompt, activity.RegisterOptions{Name: msgiface.PostApprovalPromptActivity})

	log.Printf("Starting worker on task queue %q", flags.taskQueue)
	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalf("Worker exited with error: %v", err)
	}
}

func newMessagingPlatform(flags *flags) msgiface.MessagingPlatform {
	switch flags.messagingPlatform {
	case platformSlack:
		bot, err := slack.NewSlackBot(flags.slackBotToken)
		if err != nil {
			log.Fatalf("Failed to initialise Slack bot: %v", err)
		}
		if bot.UserID != "" {
			log.Printf("Slack bot user ID: %s", bot.UserID)
		}
		return slack.NewSlackPlatform(bot.Client, bot.TeamID)

	case platformTeams:
		bot, err := teams.NewTeamsBot(flags.microsoftTenantID, flags.microsoftAppID, flags.microsoftAppPass)
		if err != nil {
			log.Fatalf("Failed to initialise Teams bot: %v", err)
		}
		log.Printf("Teams bot app ID: %s", bot.AppID)
		return teams.NewTeamsPlatform(bot, flags.teamsServiceURL)

	default:
		log.Fatalf("Unsupported MESSAGING_PLATFORM %q", flags.messagingPlatform)
		return nil
	}
}
