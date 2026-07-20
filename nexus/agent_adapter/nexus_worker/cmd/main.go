// Go Nexus service handler for the agent-adapter.
//
// Exposes AgentService with sendMessage and pollMessages operations.
// The pollMessages operation uses update-with-callback natively in Go,
// which is why this module exists instead of using the Python SDK.
//
// The agent workflow itself still runs on the Python worker (AGENT_TASK_QUEUE).
// This module only handles the Nexus service layer.
//
// Env vars:
//
//	TEMPORAL_ADDRESS           gRPC address (default: localhost:7233)
//	AGENT_NAMESPACE            namespace where the agent workflow runs (default: default)
//	AGENT_WORKFLOW_NAME        registered Temporal workflow type name (required)
//	AGENT_WORKFLOW_ID_PREFIX   prefix prepended to session IDs to form workflow IDs (default: "agent-")
//	AGENT_TASK_QUEUE           task queue where the agent workflow runs (default: "agent")
//	NEXUS_AGENT_TASK_QUEUE     task queue this worker listens on (default: nexus-agent-go)
package main

import (
	"log"
	"os"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"

	"github.com/temporal-community/temporal-agent-harness/nexus/agent_adapter/nexus_worker/handler"
)

func envOrDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	address := envOrDefault("TEMPORAL_ADDRESS", "localhost:7233")
	agentNamespace := envOrDefault("AGENT_NAMESPACE", "default")
	agentWorkflowName := os.Getenv("AGENT_WORKFLOW_NAME")
	workflowIDPrefix  := envOrDefault("AGENT_WORKFLOW_ID_PREFIX", "agent-")
	agentTaskQueue    := envOrDefault("AGENT_TASK_QUEUE", "agent")
	nexusTaskQueue := envOrDefault("NEXUS_AGENT_TASK_QUEUE", "nexus-agent-go")

	if agentWorkflowName == "" {
		log.Fatal("AGENT_WORKFLOW_NAME is required")
	}

	tc, err := client.Dial(client.Options{
		HostPort:  address,
		Namespace: agentNamespace,
	})
	if err != nil {
		log.Fatalf("Failed to create Temporal client: %v", err)
	}
	defer tc.Close()

	w := worker.New(tc, nexusTaskQueue, worker.Options{
		// Only handle Nexus tasks — workflow and activity tasks stay with
		// the Python worker on the same queue.
		DisableWorkflowWorker: true,
	})
	w.RegisterNexusService(handler.NewAgentNexusService(handler.Config{
		AgentTaskQueue:          agentTaskQueue,
		WorkflowName:            agentWorkflowName,
		WorkflowIDPrefix:        workflowIDPrefix,
		IsMessageQueuingEnabled: true,
	}))

	log.Printf("nexus-agent-go ready: namespace=%s nexusQueue=%s agentQueue=%s workflow=%s idPrefix=%s",
		agentNamespace, nexusTaskQueue, agentTaskQueue, agentWorkflowName, workflowIDPrefix)

	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalf("Worker error: %v", err)
	}
}
