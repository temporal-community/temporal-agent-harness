package main

import (
	"log"
	"os"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound/driver/teams"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/driver/temporal_agent_harness"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/temporal"
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

	inboundDriver := teams.NewDriver(workflow.ActivityOptions{
		// Teams activities may wait inside an attempt to honor Retry-After.
		StartToCloseTimeout: 5 * time.Minute,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
	})
	outboundDriver := &temporal_agent_harness.Driver{}
	routerWorkflow := router.NewRouterWorkflow(inboundDriver, outboundDriver)
	w := worker.New(tc, flags.taskQueue, worker.Options{
		// Outbound Teams activities are registered by the Python SDK worker on
		// this same task queue. Do not let this workflow worker poll and reject
		// those Activity Tasks as unregistered.
		LocalActivityWorkerOnly: true,
	})
	w.RegisterWorkflowWithOptions(routerWorkflow.Run, workflow.RegisterOptions{Name: router.WorkflowName})

	log.Printf("Starting Teams workflow worker on task queue %q", flags.taskQueue)
	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalf("Worker exited with error: %v", err)
	}
}
