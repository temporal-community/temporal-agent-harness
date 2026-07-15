package main

import (
	"log"
	"os"

	agentiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/agent"
	"github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/connector"
	msgiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging"
	"github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging/slack"
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
	platform := slack.NewSlackPlatform(bot.Client, bot.TeamID)

	c := connector.NewConnectorWorkflow(&agentiface.TemporalNativeHarnessDriver{})
	w := worker.New(tc, flags.taskQueue, worker.Options{})
	w.RegisterWorkflowWithOptions(c.Run, workflow.RegisterOptions{Name: connector.WorkflowName})
	w.RegisterActivityWithOptions(platform.BeginStream, activity.RegisterOptions{Name: msgiface.BeginStreamActivity})
	w.RegisterActivityWithOptions(platform.UpdateStream, activity.RegisterOptions{Name: msgiface.UpdateStreamActivity})
	w.RegisterActivityWithOptions(platform.FinishStream, activity.RegisterOptions{Name: msgiface.FinishStreamActivity})
	w.RegisterActivityWithOptions(platform.PostMessage, activity.RegisterOptions{Name: msgiface.PostMessageActivity})
	w.RegisterActivityWithOptions(platform.PostApprovalPrompt, activity.RegisterOptions{Name: msgiface.PostApprovalPromptActivity})

	log.Printf("Starting Slack worker on task queue %q", flags.taskQueue)
	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalf("Worker exited with error: %v", err)
	}
}
