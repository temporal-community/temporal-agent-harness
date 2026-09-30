package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestStartBodySetsEndpointAndService(t *testing.T) {
	body, err := startBody(strings.NewReader(`{"operation":"PollMessages","input":{}}`), "agent-endpoint")
	require.NoError(t, err)
	var fields map[string]any
	require.NoError(t, json.Unmarshal(body, &fields))
	require.Equal(t, "agent-endpoint", fields["endpoint"])
	require.Equal(t, "AgentService", fields["service"])
	require.Equal(t, "PollMessages", fields["operation"])
}

func TestStartBodyRejectsOtherFields(t *testing.T) {
	for _, field := range []string{"endpoint", "service", "completionCallbacks", "links", "nexusHeader"} {
		_, err := startBody(strings.NewReader(`{"operation":"PollMessages","`+field+`":null}`), "agent-endpoint")
		require.Error(t, err, field)
	}
}

func TestStartBodyRejectsOtherOperations(t *testing.T) {
	for _, operation := range []string{"ExecuteOperatorCommand", "QueryOperatorInterface", ""} {
		_, err := startBody(strings.NewReader(`{"operation":"`+operation+`"}`), "agent-endpoint")
		require.Error(t, err, operation)
	}
}

func TestRestrictAllowsOnlyRegisteredAgentsAndReads(t *testing.T) {
	var forwarded string
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			body, _ := io.ReadAll(r.Body)
			forwarded = string(body)
		}
		w.WriteHeader(http.StatusTeapot)
	})
	handler := restrict(next, map[string]string{"NexusHelloAgent": "nexus-hello-agent-endpoint"})
	for _, tc := range []struct {
		method, path, body string
		status             int
	}{
		{http.MethodPost, "/operations/op-1?agent=NexusHelloAgent", `{"operation":"QueryAgentStatus"}`, http.StatusTeapot},
		{http.MethodPost, "/operations/op-1?agent=OtherAgent", `{"operation":"QueryAgentStatus"}`, http.StatusBadRequest},
		{http.MethodPost, "/operations/op-1", `{"operation":"QueryAgentStatus"}`, http.StatusBadRequest},
		{http.MethodGet, "/operations/op-1", "", http.StatusTeapot},
		{http.MethodGet, "/operations/op-1/poll", "", http.StatusTeapot},
		{http.MethodGet, "/operations", "", http.StatusNotFound},
		{http.MethodGet, "/operation-count", "", http.StatusNotFound},
		{http.MethodPost, "/operations/op-1/cancel", "", http.StatusMethodNotAllowed},
		{http.MethodPost, "/operations/op-1/terminate", "", http.StatusMethodNotAllowed},
	} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body)))
		require.Equal(t, tc.status, recorder.Code, tc.method+" "+tc.path)
	}
	require.Contains(t, forwarded, `"endpoint":"nexus-hello-agent-endpoint"`)
}

func TestInjectConfigAddsTransportAndAgents(t *testing.T) {
	index, err := injectConfig([]byte("<html><head></head></html>"), []agentView{{Key: "k", WorkflowType: "W"}})
	require.NoError(t, err)
	require.Contains(t, string(index), `window.__AGENT_HARNESS_TRANSPORT__ = "connector"`)
	require.Contains(t, string(index), `"workflow_type":"W"`)
}
