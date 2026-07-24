package main

import (
	"log"
	"net/http"
	"os"

	teamswebhook "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound/driver/teams/webhook"
	"go.temporal.io/sdk/client"
)

type flags struct {
	temporalAddress    string
	connectorNamespace string
	taskQueue          string
	webhookPort        string
}

func ensureFlags() *flags {
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

	handler := teamswebhook.NewServer(tc, flags.taskQueue)
	addr := ":" + flags.webhookPort
	log.Printf("Teams webhook server listening on %s", addr)
	if err := http.ListenAndServe(addr, handler); err != nil {
		log.Fatalf("Webhook server error: %v", err)
	}
}
