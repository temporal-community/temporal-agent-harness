package main

import (
	"log"
	"net/http"
	"os"

	"github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging/teams"
	teamswebhook "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging/teams/webhook"
	"go.temporal.io/sdk/client"
)

type flags struct {
	microsoftTenantID  string
	microsoftAppID     string
	microsoftAppPass   string
	teamsServiceURL    string
	temporalAddress    string
	connectorNamespace string
	taskQueue          string
	webhookPort        string
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
	webhookPort := os.Getenv("WEBHOOK_PORT")
	if webhookPort == "" {
		webhookPort = "8080"
	}
	return &flags{
		microsoftTenantID:  microsoftTenantID,
		microsoftAppID:     microsoftAppID,
		microsoftAppPass:   microsoftAppPass,
		teamsServiceURL:    os.Getenv("TEAMS_SERVICE_URL"),
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

	bot, err := teams.NewTeamsBot(flags.microsoftTenantID, flags.microsoftAppID, flags.microsoftAppPass)
	if err != nil {
		log.Fatalf("Failed to initialise Teams bot: %v", err)
	}
	log.Printf("Teams bot app ID: %s", bot.AppID)
	platform := teams.NewTeamsPlatform(bot, flags.teamsServiceURL)

	handler := teamswebhook.NewServer(tc, flags.taskQueue, platform)
	addr := ":" + flags.webhookPort
	log.Printf("Teams webhook server listening on %s", addr)
	if err := http.ListenAndServe(addr, handler); err != nil {
		log.Fatalf("Webhook server error: %v", err)
	}
}
