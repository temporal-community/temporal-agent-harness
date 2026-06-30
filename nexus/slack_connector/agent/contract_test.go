package agent

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

var connectorFixtureSpecs = map[string]any{
	"stream_item_reply_delta": map[string]any{
		"turn_id": "turn-abc-123", "turn_number": 1, "timestamp": 1700000000.0,
		"event": map[string]any{"type": "reply_delta", "text": "hello world"},
	},
	"stream_item_reply": map[string]any{
		"turn_id": "turn-abc-123", "turn_number": 1, "timestamp": 1700000001.0,
		"event": map[string]any{"type": "reply", "text": "hello world, full response"},
	},
	"stream_item_tool_start": map[string]any{
		"turn_id": "turn-abc-123", "turn_number": 1, "timestamp": 1700000000.5,
		"event": map[string]any{"type": "tool_start", "tool_name": "search"},
	},
	"stream_item_tool_end": map[string]any{
		"turn_id": "turn-abc-123", "turn_number": 1, "timestamp": 1700000000.6,
		"event": map[string]any{"type": "tool_end", "tool_name": "search"},
	},
	"stream_item_error": map[string]any{
		"turn_id": "turn-abc-123", "turn_number": 1, "timestamp": 1700000002.0,
		"event": map[string]any{"type": "error", "message": "something went wrong"},
	},
}

// fixtureDir is set by TestMain when the Python generator runs successfully.
var fixtureDir string

func TestMain(m *testing.M) {
	dir, err := runContractFixtureGenerator(connectorFixtureSpecs)
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

func runContractFixtureGenerator(specs map[string]any) (string, error) {
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
	dir, err := os.MkdirTemp("", "connector-fixtures-*")
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

// -- Stream item / TurnEvent contract tests ----------------------------------
// TODO: unify this with some schema, we shouldn't duplicate them between Python and Go parts
//       of the agent. In the meantime these tests are stopgap regression tests.

func TestStreamItemContract_ReplyDelta(t *testing.T) {
	var si streamItem
	require.NoError(t, json.Unmarshal(loadFixture(t, "stream_item_reply_delta"), &si))
	assert.Equal(t, "turn-abc-123", si.TurnID)
	assert.Equal(t, 1, si.TurnNumber)
	assert.Equal(t, "reply_delta", si.Event.Type)
	assert.Equal(t, "hello world", si.Event.Text)
}

func TestStreamItemContract_Reply(t *testing.T) {
	var si streamItem
	require.NoError(t, json.Unmarshal(loadFixture(t, "stream_item_reply"), &si))
	assert.Equal(t, "reply", si.Event.Type)
	assert.NotEmpty(t, si.Event.Text)
}

func TestStreamItemContract_ToolStart(t *testing.T) {
	var si streamItem
	require.NoError(t, json.Unmarshal(loadFixture(t, "stream_item_tool_start"), &si))
	assert.Equal(t, "tool_start", si.Event.Type)
	assert.Equal(t, "search", si.Event.ToolName)
}

func TestStreamItemContract_Error(t *testing.T) {
	var si streamItem
	require.NoError(t, json.Unmarshal(loadFixture(t, "stream_item_error"), &si))
	assert.Equal(t, "error", si.Event.Type)
	assert.Equal(t, "something went wrong", si.Event.Message)
}

// TestStreamItemContract_TurnEventToDelta verifies the full decode pipeline:
// fixture JSON → streamItem → turnEvent → agentDelta.
func TestStreamItemContract_TurnEventToDelta(t *testing.T) {
	cases := []struct {
		fixture   string
		wantType  string
		wantFinal bool
		wantText  bool
	}{
		{"stream_item_reply_delta", "reply_delta", false, true},
		{"stream_item_reply", "reply", true, false}, // text already streamed via deltas
		{"stream_item_tool_start", "tool_start", false, true},
		{"stream_item_error", "error", true, true},
	}
	for _, tc := range cases {
		t.Run(tc.fixture, func(t *testing.T) {
			var si streamItem
			require.NoError(t, json.Unmarshal(loadFixture(t, tc.fixture), &si))
			assert.Equal(t, tc.wantType, si.Event.Type)

			d := turnEventToDelta(si.Event)
			require.NotNil(t, d, "turnEventToDelta returned nil for %s", tc.wantType)
			assert.Equal(t, tc.wantFinal, d.IsFinal)
			if tc.wantText {
				assert.NotEmpty(t, d.Text)
			}
		})
	}
}
