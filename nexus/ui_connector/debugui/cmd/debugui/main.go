// Command debugui runs the debug UI's Nexus-fronted connector as a single process: one
// HTTP server (serving the built Svelte UI plus the JSON/SSE API) and one Temporal
// worker (hosting RouterWorkflow - once per registered agent - plus every debuguiadmin
// workflow), sharing one in-memory event broker directly.
//
// This is the "remote/decoupled" way to run the debug UI, for an agent that isn't
// colocated with this process (e.g. examples/qa_agent) - a local example that runs its
// own agent worker in the same checkout should keep using the existing
// temporal_agent_harness.web FastAPI app instead, which needs no Nexus endpoint at all.
// See nexus/ui_connector/README.md for when to use which.
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/agent"
	debuguiadmin "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/debugui/admin"
	debuguiinbound "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/debugui/inbound"
	debuguioutbound "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/debugui/outbound"
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
	identity           string
	webhookPort        string
	registryFile       string
	staticDir          string
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
		taskQueue = "nexus-connector-debugui"
	}
	identity := os.Getenv("CONNECTOR_IDENTITY")
	webhookPort := os.Getenv("WEBHOOK_PORT")
	if webhookPort == "" {
		webhookPort = "8080"
	}
	registryFile := os.Getenv("DEBUGUI_REGISTRY_FILE")
	if registryFile == "" {
		registryFile = "agents.json"
	}
	// The built Svelte UI (ui/dist after `pnpm run build`); empty runs API-only.
	staticDir := os.Getenv("DEBUGUI_STATIC_DIR")

	return &flags{
		temporalAddress:    temporalAddress,
		connectorNamespace: connectorNamespace,
		taskQueue:          taskQueue,
		identity:           identity,
		webhookPort:        webhookPort,
		registryFile:       registryFile,
		staticDir:          staticDir,
	}
}

// registryEntry is agents.json's on-disk shape: [{"key":...,"workflow_type":...,...}, ...].
type registryEntry struct {
	Key           string `json:"key"`
	WorkflowType  string `json:"workflow_type"`
	TaskQueue     string `json:"task_queue"`
	Label         string `json:"label"`
	Description   string `json:"description"`
	NexusEndpoint string `json:"nexus_endpoint"`
}

func loadRegistry(path string) debuguiinbound.Registry {
	data, err := os.ReadFile(path)
	if err != nil {
		log.Fatalf("Failed to read registry file %q: %v", path, err)
	}
	var entries []registryEntry
	if err := json.Unmarshal(data, &entries); err != nil {
		log.Fatalf("Failed to parse registry file %q: %v", path, err)
	}
	if len(entries) == 0 {
		log.Fatalf("Registry file %q lists no agents", path)
	}
	registry := make(debuguiinbound.Registry, 0, len(entries))
	for _, e := range entries {
		if e.Key == "" || e.WorkflowType == "" || e.NexusEndpoint == "" {
			log.Fatalf("Registry entry %+v is missing key/workflow_type/nexus_endpoint", e)
		}
		registry = append(registry, debuguiinbound.AgentConfig{
			Key: e.Key, WorkflowType: e.WorkflowType, TaskQueue: e.TaskQueue,
			Label: e.Label, Description: e.Description, NexusEndpoint: e.NexusEndpoint,
		})
	}
	return registry
}

func main() {
	f := ensureFlags()
	registry := loadRegistry(f.registryFile)

	tc, err := client.Dial(client.Options{
		HostPort:  f.temporalAddress,
		Namespace: f.connectorNamespace,
	})
	if err != nil {
		log.Fatalf("Failed to connect to Temporal: %v", err)
	}
	defer tc.Close()

	broker := debuguioutbound.NewBroker()
	outboundDriver := debuguioutbound.NewDriver(workflow.ActivityOptions{
		StartToCloseTimeout: 10 * time.Second,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 3},
	})

	w := worker.New(tc, f.taskQueue, worker.Options{})

	// One RouterWorkflow registration per registered agent type - see
	// debuguiinbound.RouterWorkflowName's doc comment for why.
	for _, a := range registry {
		backendDriver := &agent.Driver{NexusEndpoint: a.NexusEndpoint}
		routerWorkflow := router.NewRouterWorkflow(outboundDriver, backendDriver)
		w.RegisterWorkflowWithOptions(routerWorkflow.Run, workflow.RegisterOptions{
			Name: debuguiinbound.RouterWorkflowName(a.Key),
		})
	}
	debuguiadmin.Register(w, broker)

	go func() {
		log.Printf("Starting debugui worker on task queue %q", f.taskQueue)
		if err := w.Run(worker.InterruptCh()); err != nil {
			log.Fatalf("Worker exited with error: %v", err)
		}
	}()

	staticDir := debuguiinbound.ResolvePackagedUI(f.staticDir)
	if f.staticDir != "" && staticDir == "" {
		log.Printf("DEBUGUI_STATIC_DIR=%q has no index.html - serving API only", f.staticDir)
	}
	handler := debuguiinbound.NewServer(tc, f.taskQueue, f.identity, registry, broker, staticDir)
	addr := ":" + f.webhookPort
	log.Printf("debugui HTTP server listening on %s", addr)
	if err := http.ListenAndServe(addr, handler); err != nil {
		log.Fatalf("HTTP server error: %v", err)
	}
}
