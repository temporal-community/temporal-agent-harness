// Package nexusinteg contains end-to-end durability tests for the Nexus-based
// AI agent pipeline. Tests require an embedded Temporal dev server and are
// gated behind the "integration" build tag.
//
// Run all tests:
//
//	go test -tags integration -timeout 300s -race -count=1 ./...
package nexusinteg

import (
	"context"
	"io"
	"strings"
	"testing"
	"time"

	nexusv1 "go.temporal.io/api/nexus/v1"
	operatorv1 "go.temporal.io/api/operatorservice/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/testsuite"

	"github.com/stretchr/testify/require"
)

// NewDevServer starts an embedded Temporal CLI dev server for integration
// testing. The test is skipped (not failed) if the binary cannot start, so
// environments without network access won't block CI.
func NewDevServer(t *testing.T) *testsuite.DevServer {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	srv, err := testsuite.StartDevServer(ctx, testsuite.DevServerOptions{
		ClientOptions: &client.Options{Namespace: "default"},
		LogLevel:      "warn",
		Stdout:        io.Discard,
		Stderr:        io.Discard,
	})
	cancel()
	if err != nil {
		t.Skipf("integration test skipped: cannot start dev server: %v", err)
	}
	t.Cleanup(func() { _ = srv.Stop() })
	return srv
}

// CreateNexusEndpoint registers a Nexus HTTP endpoint routing to taskQueue
// in the default namespace.
func CreateNexusEndpoint(t *testing.T, tc client.Client, endpointName, taskQueue string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	_, err := tc.OperatorService().CreateNexusEndpoint(ctx, &operatorv1.CreateNexusEndpointRequest{
		Spec: &nexusv1.EndpointSpec{
			Name: endpointName,
			Target: &nexusv1.EndpointTarget{
				Variant: &nexusv1.EndpointTarget_Worker_{
					Worker: &nexusv1.EndpointTarget_Worker{
						Namespace: "default",
						TaskQueue: taskQueue,
					},
				},
			},
		},
	})
	require.NoError(t, err)
}

// TaskQueue returns a task-queue name unique per test to prevent cross-test
// interference when tests share a server instance.
func TaskQueue(t *testing.T, prefix string) string {
	t.Helper()
	return prefix + strings.ReplaceAll(t.Name(), "/", "-")
}
