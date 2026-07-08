// Worker hosting SubagentService, fronting one harness agent (running separately, on
// AGENT_TASK_QUEUE) as an A2A-shaped subagent.
//
// Env vars:
//
//	TEMPORAL_ADDRESS           gRPC address (default: localhost:7233)
//	AGENT_NAMESPACE            namespace where the agent workflow runs (default: default)
//	AGENT_WORKFLOW_NAME        registered Temporal workflow type name (required)
//	AGENT_WORKFLOW_ID_PREFIX   prefix prepended to task ids to form workflow ids (default: "subagent-")
//	AGENT_TASK_QUEUE           task queue where the agent workflow runs (default: "agent")
//	NEXUS_SUBAGENT_TASK_QUEUE  task queue this worker listens on (default: nexus-subagent-go)
package main

import (
	"log"
	"os"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/worker"

	"github.com/temporal-community/temporal-agent-harness/nexus/subagent_adapter/nexus_worker/handler"
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
	workflowIDPrefix := envOrDefault("AGENT_WORKFLOW_ID_PREFIX", "subagent-")
	agentTaskQueue := envOrDefault("AGENT_TASK_QUEUE", "agent")
	nexusTaskQueue := envOrDefault("NEXUS_SUBAGENT_TASK_QUEUE", "nexus-subagent-go")

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
		DisableWorkflowWorker: true,
	})
	w.RegisterNexusService(handler.NewSubagentNexusService(handler.Config{
		AgentTaskQueue:          agentTaskQueue,
		WorkflowName:            agentWorkflowName,
		WorkflowIDPrefix:        workflowIDPrefix,
		IsMessageQueuingEnabled: true,
	}))

	log.Printf("nexus-subagent-go ready: namespace=%s nexusQueue=%s agentQueue=%s workflow=%s idPrefix=%s",
		agentNamespace, nexusTaskQueue, agentTaskQueue, agentWorkflowName, workflowIDPrefix)

	if err := w.Run(worker.InterruptCh()); err != nil {
		log.Fatalf("Worker error: %v", err)
	}
}
