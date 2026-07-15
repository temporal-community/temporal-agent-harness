package main

import (
	"log"
	"os"

	agentiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/agent"
	"github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/connector"
	msgiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging"
	"github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging/teams"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

type flags struct {
	microsoftTenantID  string
	microsoftAppID     string
	microsoftAppPass   string
	teamsServiceURL    string
	temporalAddress    string
	connectorNamespace string
	taskQueue          string
}

func ensureFlags() *flags {
	microsoftTenantID := os.Getenv("MICROSOFT_TENANT_ID")
	if microsoftTenantID == "" {
		log.Fatal("MICROSOFT_TENANT_ID is required")
	}
	microsoftAppID := os.Getenv("MICROSOFT_APP_ID")
	if microsoftAppID == "" {
		log.Fatal("MICROSOFT_APP_ID is required")
	}
	microsoftAppPass := os.Getenv("MICROSOFT_APP_PASSWORD")
	if microsoftAppPass == "" {
		log.Fatal("MICROSOFT_APP_PASSWORD is required")
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
		taskQueue = "nexus-connector-teams"
	}
	return &flags{
		microsoftTenantID:  microsoftTenantID,
		microsoftAppID:     microsoftAppID,
		microsoftAppPass:   microsoftAppPass,
		teamsServiceURL:    os.Getenv("TEAMS_SERVICE_URL"),
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

	bot, err := teams.NewTeamsBot(flags.microsoftTenantID, flags.microsoftAppID, flags.microsoftAppPass)
	if err != nil {
		log.Fatalf("Failed to initialise Teams bot: %v", err)
	}
	log.Printf("Teams bot app ID: %s", bot.AppID)
	platform := teams.NewTeamsPlatform(bot, flags.teamsServiceURL)

	c := connector.NewConnectorWorkflow(&agentiface.TemporalNativeHarnessDriver{})
	w := worker.New(tc, flags.taskQueue, worker.Options{})
	w.RegisterWorkflowWithOptions(c.Run, workflow.RegisterOptions{Name: connector.WorkflowName})
	w.RegisterActivityWithOptions(platform.BeginStream, activity.RegisterOptions{Name: msgiface.BeginStreamActivity})
	w.RegisterActivityWithOptions(platform.UpdateStream, activity.RegisterOptions{Name: msgiface.UpdateStreamActivity})
	w.RegisterActivityWithOptions(platform.FinishStream, activity.RegisterOptions{Name: msgiface.FinishStreamActivity})
	w.RegisterActivityWithOptions(platform.PostMessage, activity.RegisterOptions{Name: msgiface.PostMessageActivity})
	w.RegisterActivityWithOptions(platform.PostApprovalPrompt, activity.RegisterOptions{Name: msgiface.PostApprovalPromptActivity})

	log.Printf("Starting Teams worker on task queue %q", flags.taskQueue)
	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalf("Worker exited with error: %v", err)
	}
}
