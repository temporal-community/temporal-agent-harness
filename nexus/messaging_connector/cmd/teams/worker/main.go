package main

import (
	"log"
	"os"

	agentiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/agent"
	"github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/connector"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

type flags struct {
	temporalAddress    string
	connectorNamespace string
	taskQueue          string
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
	return &flags{
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

	c := connector.NewConnectorWorkflow(&agentiface.TemporalNativeHarnessDriver{})
	w := worker.New(tc, flags.taskQueue, worker.Options{
		// Outbound Teams activities are registered by the Python SDK worker on
		// this same task queue. Do not let this workflow worker poll and reject
		// those Activity Tasks as unregistered.
		LocalActivityWorkerOnly: true,
	})
	w.RegisterWorkflowWithOptions(c.Run, workflow.RegisterOptions{Name: connector.WorkflowName})

	log.Printf("Starting Teams workflow worker on task queue %q", flags.taskQueue)
	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalf("Worker exited with error: %v", err)
	}
}
