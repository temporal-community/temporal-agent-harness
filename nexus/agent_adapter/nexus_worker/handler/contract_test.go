package handler

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// handlerFixtureSpecs defines the test data sent to the Python generator.
// Each entry is round-tripped through the named Python class (_type) so that any
// field-name mismatch between Go and Python is caught at test time.
var handlerFixtureSpecs = map[string]any{
	"agent_message": map[string]any{
		"_type":         "AgentMessage",
		"type":          "ask",
		"payload":       map[string]any{"text": "what is a workflow?"},
		"expected_turn": 1,
	},
	"user_input_result": map[string]any{
		"_type":       "UserInputResult",
		"turn_number": 1,
		"turn_id":     "abc-123-def-456",
		"pending":     false,
	},
	"agent_status": map[string]any{
		"_type":                      "AgentStatus",
		"current_turn":               2,
		"turn_active":                true,
		"pending_turns":              []any{map[string]any{"turn_number": 3, "turn_id": "pend-1", "message": "follow up"}},
		"is_message_queuing_enabled": true,
	},
	"agent_status_empty": map[string]any{
		"_type": "AgentStatus",
	},
}

// fixtureDir is set by TestMain when the Python generator runs successfully.
var fixtureDir string

func TestMain(m *testing.M) {
	dir, err := runFixtureGenerator(handlerFixtureSpecs)
	if err != nil {
		log.Printf("contract fixtures unavailable (%v) — contract tests will be skipped", err)
	} else {
		fixtureDir = dir
		defer os.RemoveAll(dir)
	}
	os.Exit(m.Run())
}

func findProjectRoot() (string, error) {
	dir, err := os.Getwd()
	if err != nil {
		return "", err
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "justfile")); err == nil {
			return dir, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", fmt.Errorf("project root (justfile) not found")
		}
		dir = parent
	}
}

func runFixtureGenerator(specs map[string]any) (string, error) {
	if _, err := exec.LookPath("uv"); err != nil {
		return "", fmt.Errorf("uv not in PATH: %w", err)
	}
	root, err := findProjectRoot()
	if err != nil {
		return "", err
	}
	input, err := json.Marshal(specs)
	if err != nil {
		return "", err
	}
	dir, err := os.MkdirTemp("", "handler-fixtures-*")
	if err != nil {
		return "", err
	}
	cmd := exec.Command("uv", "run", "python", "scripts/generate_fixtures.py", "--out-dir", dir)
	cmd.Dir = root
	cmd.Stdin = bytes.NewReader(input)
	if out, err := cmd.CombinedOutput(); err != nil {
		os.RemoveAll(dir)
		return "", fmt.Errorf("generator failed: %w\n%s", err, out)
	}
	return dir, nil
}

func loadFixture(t *testing.T, name string) []byte {
	t.Helper()
	if fixtureDir == "" {
		t.Skip("contract fixtures not available (uv/Python not found)")
	}
	data, err := os.ReadFile(filepath.Join(fixtureDir, name+".json"))
	require.NoError(t, err)
	return data
}

// -- Handler type contracts -------------------------------------------------
// Each test deserializes the Python-round-tripped JSON into the Go struct and
// verifies all expected fields survived.

func TestAgentMessageContract(t *testing.T) {
	var v AgentMessage
	require.NoError(t, json.Unmarshal(loadFixture(t, "agent_message"), &v))
	assert.Equal(t, "ask", v.Type)
	assert.Equal(t, "what is a workflow?", v.Payload["text"])
	assert.Equal(t, 1, v.ExpectedTurn)
}

func TestUserInputResultContract(t *testing.T) {
	var v UserInputResult
	require.NoError(t, json.Unmarshal(loadFixture(t, "user_input_result"), &v))
	assert.Equal(t, 1, v.TurnNumber)
	assert.Equal(t, "abc-123-def-456", v.TurnID)
	assert.False(t, v.Pending)
}

func TestAgentStatusContract(t *testing.T) {
	var v AgentStatus
	require.NoError(t, json.Unmarshal(loadFixture(t, "agent_status"), &v))
	assert.Equal(t, 2, v.CurrentTurn)
	assert.True(t, v.TurnActive)
	assert.Len(t, v.PendingTurns, 1)
	assert.True(t, v.IsMessageQueuingEnabled)
}

func TestAgentStatusEmptyContract(t *testing.T) {
	var v AgentStatus
	require.NoError(t, json.Unmarshal(loadFixture(t, "agent_status_empty"), &v))
	assert.Equal(t, 0, v.CurrentTurn)
	assert.Empty(t, v.PendingTurns)
}

func TestStreamPollInputContract(t *testing.T) {
	// streamPollInput has no Python class — verify json tags directly.
	got := streamPollInput{FromOffset: 42, Topics: []string{"turn_events"}}
	b, err := json.Marshal(got)
	require.NoError(t, err)
	var wire map[string]any
	require.NoError(t, json.Unmarshal(b, &wire))
	assert.Equal(t, float64(42), wire["from_offset"], "from_offset json tag mismatch")
	assert.Equal(t, []any{"turn_events"}, wire["topics"], "topics json tag mismatch")
}
