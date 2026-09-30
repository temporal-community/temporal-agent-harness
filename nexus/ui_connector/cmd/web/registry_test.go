package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestLoadAgentsKeepsOnlyAgentsWithNexusEndpoint(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agents.toml")
	require.NoError(t, os.WriteFile(path, []byte(`
[[agents]]
key = "a"
workflow_type = "AgentA"
task_queue = "a"
label = "A"
description = """
two
lines"""
nexus_endpoint = "a-endpoint"

[[agents]]
key = "b"
workflow_type = "AgentB"
task_queue = "b"
label = "B"
description = "no endpoint"
`), 0o600))

	agents, err := LoadAgents([]string{path})
	require.NoError(t, err)
	require.Len(t, agents, 1)
	require.Equal(t, "AgentA", agents[0].WorkflowType)
	require.Equal(t, "a-endpoint", agents[0].NexusEndpoint)
	require.Equal(t, "two lines", agents[0].Description)
}

func TestLoadAgentsFailsWithoutNexusEndpoint(t *testing.T) {
	path := filepath.Join(t.TempDir(), "agents.toml")
	require.NoError(t, os.WriteFile(path, []byte(`
[[agents]]
key = "b"
workflow_type = "AgentB"
task_queue = "b"
label = "B"
description = "no endpoint"
`), 0o600))

	_, err := LoadAgents([]string{path})
	require.Error(t, err)
}
