package main

import (
	"log"
	"os"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack/outbound"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"
)

type flags struct {
	slackBotToken      string
	temporalAddress    string
	connectorNamespace string
	taskQueue          string
}

func ensureFlags() *flags {
	slackBotToken := os.Getenv("SLACK_BOT_TOKEN")
	if slackBotToken == "" {
		log.Fatal("SLACK_BOT_TOKEN is required")
	}

	temporalAddress := os.Getenv("TEMPORAL_ADDRESS")
	if temporalAddress == "" {
		temporalAddress = "localhost:7233"
	}
	connectorNamespace := os.Getenv("CONNECTOR_NAMESPACE")
	if connectorNamespace == "" {
		connectorNamespace = "connector"
	}
	taskQueue := os.Getenv("SLACK_DRIVER_TASK_QUEUE")
	if taskQueue == "" {
		taskQueue = "nexus-connector-slack"
	}
	return &flags{
		slackBotToken:      slackBotToken,
		temporalAddress:    temporalAddress,
		connectorNamespace: connectorNamespace,
		taskQueue:          taskQueue,
	}
}

func main() {
	flags := ensureFlags()

	tc, err := client.Dial(client.Options{
		HostPort:  flags.temporalAddress,
		Namespace: flags.connectorNamespace,
	})
	if err != nil {
		log.Fatalf("Failed to create Temporal client: %v", err)
	}
	defer tc.Close()

	bot, err := slack.NewSlackBot(flags.slackBotToken)
	if err != nil {
		log.Fatalf("Failed to initialise Slack bot: %v", err)
	}
	if bot.UserID != "" {
		log.Printf("Slack bot user ID: %s", bot.UserID)
	}
	platform := slackoutbound.NewSlackPlatform(bot.Client, bot.TeamID)
	w := worker.New(tc, flags.taskQueue, worker.Options{})
	slackoutbound.RegisterActivities(w, platform)

	log.Printf("Starting Slack worker on task queue %q", flags.taskQueue)
	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalf("Worker exited with error: %v", err)
	}
}
