// Command tunnel runs the shared, driver-neutral UI tunnel workflow worker.
package main

import (
	"log"
	"os"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/agent"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func main() {
	namespace := env("CONNECTOR_NAMESPACE", "connector")
	taskQueue := env("CONNECTOR_TASK_QUEUE", "nexus-ui-tunnel")
	tc, err := client.Dial(client.Options{
		HostPort:  env("TEMPORAL_ADDRESS", "localhost:7233"),
		Namespace: namespace,
	})
	if err != nil {
		log.Fatalf("create Temporal client: %v", err)
	}
	defer tc.Close()

	w := worker.New(tc, taskQueue, worker.Options{LocalActivityWorkerOnly: true})
	tunnel := router.NewTunnelWorkflow(agent.A2ABackend{})
	w.RegisterWorkflowWithOptions(tunnel.Run, workflow.RegisterOptions{Name: router.TunnelWorkflowName})
	log.Printf("Starting shared UI tunnel worker in namespace %q on task queue %q", namespace, taskQueue)
	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalf("worker exited: %v", err)
	}
}
