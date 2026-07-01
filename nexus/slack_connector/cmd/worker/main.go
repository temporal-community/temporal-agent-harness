package main

import (
	"log"
	"os"

	agentiface "github.com/temporalio/nexus_connector_slack/agent"
	msgiface "github.com/temporalio/nexus_connector_slack/messaging"
	"github.com/temporalio/nexus_connector_slack/messaging/slack"
	"github.com/temporalio/nexus_connector_slack/connector"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
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
	taskQueue := os.Getenv("CONNECTOR_TASK_QUEUE")
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

	tc, err := client.Dial(client.Options{HostPort: flags.temporalAddress, Namespace: flags.connectorNamespace})
	if err != nil {
		log.Fatalf("Failed to create Temporal client: %v", err)
	}
	defer tc.Close()

	bot, err := slack.NewSlackBot(flags.slackBotToken)
	if err != nil {
		log.Fatalf("Failed to initialise Slack bot: %v", err)
	}
	if bot.UserID != "" {
		log.Printf("Bot user ID: %s", bot.UserID)
	}

	driver := slack.NewSlackPlatform(bot.Client, bot.TeamID)
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
