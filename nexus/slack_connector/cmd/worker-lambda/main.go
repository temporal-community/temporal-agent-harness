// Command worker-lambda runs the Slack connector Temporal worker inside AWS
// Lambda via the lambdaworker contrib package. Connection settings are loaded
// from the environment / temporal.toml by envconfig (TEMPORAL_ADDRESS,
// TEMPORAL_NAMESPACE, TEMPORAL_TASK_QUEUE, TEMPORAL_API_KEY, TEMPORAL_TLS*).
//
// See README.md in this directory for build, deployment, and versioning notes.
package main

import (
	"os"

	"github.com/temporalio/temporal-agent-harness/nexus/slack_connector/connectorworker"

	"go.temporal.io/sdk/contrib/aws/lambdaworker"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

func getenvOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func main() {
	lambdaworker.RunWorker(worker.WorkerDeploymentVersion{
		DeploymentName: getenvOr("WORKER_DEPLOYMENT_NAME", "nexus-connector-slack"),
		BuildID:        getenvOr("WORKER_BUILD_ID", "dev"),
	}, func(o *lambdaworker.Options) error {
		// The connector workflow is short-lived; pin it to the build it starts
		// on so a new deployment never migrates in-flight runs.
		o.WorkerOptions.DeploymentOptions.DefaultVersioningBehavior = workflow.VersioningBehaviorPinned

		// Default the task queue and namespace to match the always-on worker
		// (cmd/worker) unless overridden via TEMPORAL_TASK_QUEUE / TEMPORAL_NAMESPACE.
		if o.TaskQueue == "" {
			o.TaskQueue = "nexus-connector-slack"
		}
		if o.ClientOptions.Namespace == "" {
			o.ClientOptions.Namespace = "connector"
		}

		// Source the Temporal Cloud API key and Slack bot token from Secrets
		// Manager when their *_SECRET_ARN vars are set (falling back to env).
		slackToken, err := resolveSecrets(o)
		if err != nil {
			return err
		}

		return connectorworker.Register(o, slackToken)
	})
}
