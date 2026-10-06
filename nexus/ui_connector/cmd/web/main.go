// Command web serves the packaged UI and the inbound HTTP connector for standalone
// Nexus operations. The UI calls the AgentService of each agent through /nexus/. It
// needs no FastAPI server.
//
// Env vars: TEMPORAL_ADDRESS, CONNECTOR_NAMESPACE, AGENT_REGISTRY (comma-separated
// agents.toml paths; only agents with nexus_endpoint), UI_DIST (built UI directory),
// WEB_ADDR.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	nexushttp "github.com/temporalio/nexus-inbound-http-connector"
	"go.temporal.io/sdk/client"
)

const agentServiceName = "AgentService"

// agentOperations are the AgentService operations the browser may start.
var agentOperations = map[string]bool{
	"SendAgentMessage":      true,
	"QueryAgentStatus":      true,
	"QueryAgentInterface":   true,
	"ApproveToolCall":       true,
	"ProvideCallbackResult": true,
	"PollMessages":          true,
}

// allowedStartFields are the start request fields the browser may set. The middleware
// sets endpoint and service. It rejects all other fields, for example callbacks and links.
var allowedStartFields = map[string]bool{
	"operation":              true,
	"input":                  true,
	"scheduleToCloseTimeout": true,
	"scheduleToStartTimeout": true,
}

// agentView is one agent as the UI lists it.
type agentView struct {
	Key          string `json:"key"`
	WorkflowType string `json:"workflow_type"`
	TaskQueue    string `json:"task_queue"`
	Label        string `json:"label"`
	Description  string `json:"description"`
}

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func main() {
	dist := env("UI_DIST", "../../temporal_agent_harness/ui/dist")
	addr := env("WEB_ADDR", "127.0.0.1:8080")
	registry := os.Getenv("AGENT_REGISTRY")
	if registry == "" {
		log.Fatal("AGENT_REGISTRY is required")
	}
	agents, err := LoadAgents(strings.Split(registry, ","))
	if err != nil {
		log.Fatal(err)
	}
	endpoints := map[string]string{}
	views := make([]agentView, 0, len(agents))
	for _, a := range agents {
		endpoints[a.WorkflowType] = a.NexusEndpoint
		views = append(views, agentView{
			Key: a.Key, WorkflowType: a.WorkflowType, TaskQueue: a.TaskQueue,
			Label: a.Label, Description: a.Description,
		})
	}

	connector, err := nexushttp.NewHandler(context.Background(), nexushttp.Options{
		ClientOptions: &client.Options{
			HostPort:  env("TEMPORAL_ADDRESS", "localhost:7233"),
			Namespace: env("CONNECTOR_NAMESPACE", "connector"),
			Identity:  "agent-harness-web",
		},
		Logger: slog.Default(),
	})
	if err != nil {
		log.Fatalf("create connector handler: %v", err)
	}
	defer connector.Close()

	index, err := os.ReadFile(filepath.Join(dist, "index.html"))
	if err != nil {
		log.Fatalf("read UI build (run `just app-build`): %v", err)
	}
	index, err = injectConfig(index, views)
	if err != nil {
		log.Fatal(err)
	}

	mux := http.NewServeMux()
	mux.Handle("/nexus/", http.StripPrefix("/nexus", restrict(connector, endpoints)))
	files := http.FileServer(http.Dir(dist))
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" || r.URL.Path == "/index.html" {
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.Header().Set("Cache-Control", "no-store")
			_, _ = w.Write(index)
			return
		}
		files.ServeHTTP(w, r)
	})

	log.Printf("Serving UI on http://%s with %d agents", addr, len(agents))
	log.Fatal(http.ListenAndServe(addr, mux))
}

// injectConfig adds the transport flag and the agent list to index.html. The UI then
// uses the connector client.
func injectConfig(index []byte, agents []agentView) ([]byte, error) {
	encoded, err := json.Marshal(agents)
	if err != nil {
		return nil, err
	}
	script := fmt.Sprintf(`<script>window.__AGENT_HARNESS_TRANSPORT__ = "connector"; window.__AGENT_HARNESS_AGENTS__ = %s;</script>`, encoded)
	return bytes.Replace(index, []byte("</head>"), []byte(script+"</head>"), 1), nil
}

// restrict lets the browser start AgentService operations of registered agents, and
// describe and poll operations. Nothing else reaches the connector.
func restrict(next http.Handler, endpoints map[string]string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/operations/")
		if path == r.URL.Path || path == "" {
			http.NotFound(w, r)
			return
		}
		id, suffix, _ := strings.Cut(path, "/")
		switch {
		case r.Method == http.MethodGet && (suffix == "" || suffix == "poll"):
			next.ServeHTTP(w, r)
		case r.Method == http.MethodPost && suffix == "":
			endpoint, ok := endpoints[r.URL.Query().Get("agent")]
			if !ok {
				http.Error(w, "unknown agent", http.StatusBadRequest)
				return
			}
			body, err := startBody(r.Body, endpoint)
			if err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			r.Body = io.NopCloser(bytes.NewReader(body))
			r.ContentLength = int64(len(body))
			next.ServeHTTP(w, r)
		default:
			http.Error(w, "operation "+id+": method not allowed", http.StatusMethodNotAllowed)
		}
	})
}

// startBody checks the start request fields and sets the endpoint and service.
func startBody(body io.Reader, endpoint string) ([]byte, error) {
	var fields map[string]json.RawMessage
	if err := json.NewDecoder(http.MaxBytesReader(nil, io.NopCloser(body), 4<<20)).Decode(&fields); err != nil {
		return nil, err
	}
	for name := range fields {
		if !allowedStartFields[name] {
			return nil, fmt.Errorf("start request field not allowed: %s", name)
		}
	}
	var operation string
	if err := json.Unmarshal(fields["operation"], &operation); err != nil || !agentOperations[operation] {
		return nil, fmt.Errorf("operation not allowed: %s", fields["operation"])
	}
	fields["endpoint"], _ = json.Marshal(endpoint)
	fields["service"], _ = json.Marshal(agentServiceName)
	return json.Marshal(fields)
}
