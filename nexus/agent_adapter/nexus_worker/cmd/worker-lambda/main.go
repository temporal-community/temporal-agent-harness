// Command worker-lambda runs the agent-adapter Nexus worker inside AWS Lambda via
// the lambdaworker contrib package. It registers the same AgentService Nexus
// handler as the always-on cmd/main.go, but delegates the worker lifecycle to
// Lambda: Temporal Cloud invokes the function when Nexus tasks arrive, the worker
// dials Temporal, serves the operations, then drains before the deadline.
//
// This is a Nexus-only worker (DisableWorkflowWorker) — the agent workflow itself
// still runs on the Python worker (AGENT_TASK_QUEUE). Connection settings load
// from the environment / temporal.toml via envconfig (TEMPORAL_ADDRESS,
// TEMPORAL_NAMESPACE, TEMPORAL_TASK_QUEUE, TEMPORAL_API_KEY, TEMPORAL_TLS*).
//
// Agent-specific env vars (mirroring cmd/main.go):
//
//	AGENT_WORKFLOW_NAME       registered Temporal workflow type name (required)
//	AGENT_TASK_QUEUE          task queue the agent workflow runs on (default: "agent")
//	AGENT_WORKFLOW_ID_PREFIX  prefix prepended to session IDs to form workflow IDs (default: "agent-")
//
// See README.md in this directory for build, deployment, and versioning notes.
package main

import (
	"fmt"
	"os"

	"github.com/temporalio/temporal-agent-harness/nexus/agent_adapter/nexus_worker/handler"

	"go.temporal.io/sdk/contrib/aws/lambdaworker"
	"go.temporal.io/sdk/worker"
)

func getenvOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	lambdaworker.RunWorker(worker.WorkerDeploymentVersion{
		DeploymentName: getenvOr("WORKER_DEPLOYMENT_NAME", "nexus-agent-go"),
		BuildID:        getenvOr("WORKER_BUILD_ID", "dev"),
	}, func(o *lambdaworker.Options) error {
		workflowName := os.Getenv("AGENT_WORKFLOW_NAME")
		if workflowName == "" {
			return fmt.Errorf("AGENT_WORKFLOW_NAME is required")
		}

		// This worker only serves Nexus tasks; the agent workflow and its
		// activities run on the Python worker (AGENT_TASK_QUEUE).
		o.WorkerOptions.DisableWorkflowWorker = true

		// Default the Nexus task queue to match cmd/main.go's NEXUS_AGENT_TASK_QUEUE
		// default unless overridden via TEMPORAL_TASK_QUEUE. This is the queue the
		// support-agent-nexus must target.
		if o.TaskQueue == "" {
			o.TaskQueue = "nexus-agent-go"
		}

		// Source the Temporal Cloud API key from Secrets Manager when
		// TEMPORAL_API_KEY_SECRET_ARN is set (falling back to env/envconfig).
		if err := resolveSecrets(o); err != nil {
			return err
		}

		o.RegisterNexusService(handler.NewAgentNexusService(handler.Config{
			AgentTaskQueue:          getenvOr("AGENT_TASK_QUEUE", "agent"),
			WorkflowName:            workflowName,
			WorkflowIDPrefix:        getenvOr("AGENT_WORKFLOW_ID_PREFIX", "agent-"),
			IsMessageQueuingEnabled: true,
		}))
		return nil
	})
}
