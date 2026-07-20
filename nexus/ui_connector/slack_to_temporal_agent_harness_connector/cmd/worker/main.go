package main

import (
	"log"
	"os"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound/driver/slack"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/driver/temporal_agent_harness"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/temporal"
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
	// Compose this connector: a Slack inbound driver + the temporal-agent-harness
	// outbound driver, plugged into the generic RouterWorkflow.
	platform := slack.NewSlackPlatform(bot.Client, bot.TeamID)
	inboundDriver := slack.NewDriver(workflow.ActivityOptions{
		StartToCloseTimeout: 30 * time.Second,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
	})
	outboundDriver := &temporal_agent_harness.Driver{}

	w := worker.New(tc, flags.taskQueue, worker.Options{})
	routerWorkflow := router.NewRouterWorkflow(inboundDriver, outboundDriver)
	w.RegisterWorkflowWithOptions(routerWorkflow.Run, workflow.RegisterOptions{Name: router.WorkflowName})
	slack.RegisterActivities(w, platform)

	log.Printf("Starting Slack worker on task queue %q", flags.taskQueue)
	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalf("Worker exited with error: %v", err)
	}
}
