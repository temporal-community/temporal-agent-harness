package main

import (
	"log"
	"net/http"
	"os"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/agent"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/teams/inbound"
	"go.temporal.io/sdk/client"
)

type flags struct {
	temporalAddress    string
	connectorNamespace string
	taskQueue          string
	deliveryTaskQueue  string
	nexusEndpoint      string
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
		taskQueue = "nexus-ui-tunnel"
	}
	deliveryTaskQueue := os.Getenv("TEAMS_DRIVER_TASK_QUEUE")
	if deliveryTaskQueue == "" {
		deliveryTaskQueue = "nexus-connector-teams"
	}
	nexusEndpoint := os.Getenv("NEXUS_AGENT_ENDPOINT")
	if nexusEndpoint == "" {
		log.Fatal("NEXUS_AGENT_ENDPOINT is required")
	}
	webhookPort := os.Getenv("WEBHOOK_PORT")
	if webhookPort == "" {
		webhookPort = "8080"
	}
	return &flags{
		temporalAddress:    temporalAddress,
		connectorNamespace: connectorNamespace,
		taskQueue:          taskQueue,
		deliveryTaskQueue:  deliveryTaskQueue,
		nexusEndpoint:      nexusEndpoint,
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

	tunnel := router.NewClient(tc, flags.taskQueue, flags.nexusEndpoint, agent.NewA2AActions(tc, flags.nexusEndpoint))
	handler := teamsinbound.NewServer(tunnel, flags.deliveryTaskQueue)
	addr := ":" + flags.webhookPort
	log.Printf("Teams webhook server listening on %s", addr)
	if err := http.ListenAndServe(addr, handler); err != nil {
		log.Fatalf("Webhook server error: %v", err)
	}
}
