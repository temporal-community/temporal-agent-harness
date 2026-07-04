// Package connectorworker holds the shared setup for the Slack connector worker:
// building the Slack driver and registering the connector workflow and its
// activities. Both the standard worker (cmd/worker) and the Lambda worker
// (cmd/worker-lambda) call Register so they register exactly the same things.
package connectorworker

import (
	"fmt"
	"log"

	agentiface "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/agent"
	"github.com/temporalio/temporal-agent-harness/nexus/slack_connector/connector"
	msgiface "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging"
	"github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging/slack"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

// Register initialises the Slack bot and driver from slackBotToken and registers
// the connector workflow and its activities onto r. It accepts a worker.Registry
// so it works for both a standard worker.Worker and a lambdaworker.Options, which
// both implement that interface.
func Register(r worker.Registry, slackBotToken string) error {
	if slackBotToken == "" {
		return fmt.Errorf("slack bot token is required")
	}

	bot, err := slack.NewSlackBot(slackBotToken)
	if err != nil {
		return fmt.Errorf("initialise Slack bot: %w", err)
	}
	if bot.UserID != "" {
		log.Printf("Bot user ID: %s", bot.UserID)
	}

	driver := slack.NewSlackPlatform(bot.Client, bot.TeamID)
	c := connector.NewConnectorWorkflow(&agentiface.TemporalNativeHarnessDriver{})

	r.RegisterWorkflowWithOptions(c.Run, workflow.RegisterOptions{Name: connector.WorkflowName})
	r.RegisterActivityWithOptions(driver.Stream, activity.RegisterOptions{Name: msgiface.StreamActivity})
	r.RegisterActivityWithOptions(driver.PostMessage, activity.RegisterOptions{Name: msgiface.PostMessageActivity})
	r.RegisterActivityWithOptions(driver.PostApprovalPrompt, activity.RegisterOptions{Name: msgiface.PostApprovalPromptActivity})

	return nil
}
