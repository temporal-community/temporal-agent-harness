package slack

import (
	"context"
	"testing"

	msgiface "github.com/temporalio/temporal-agent-harness/nexus/slack_connector/messaging"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestParseChannel(t *testing.T) {
	cases := []struct {
		input   string
		want    string
		wantErr bool
	}{
		{"slack:C12345", "C12345", false},
		{"slack:C0B6KE9B1LJ", "C0B6KE9B1LJ", false},
		{"discord:987654", "987654", false},
		// Thread-scoped sessions carry a trailing thread root, which must be ignored.
		{"slack:C12345:1699887766.001100", "C12345", false},
		{"slack:C0B6KE9B1LJ:1783689596.364049", "C0B6KE9B1LJ", false},
		{"", "", true},
		{"nocolon", "", true},
		{"slack:", "", true},
		{"slack::1699.0001", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.input, func(t *testing.T) {
			got, err := parseChannel(tc.input)
			if tc.wantErr {
				require.Error(t, err)
			} else {
				require.NoError(t, err)
				assert.Equal(t, tc.want, got)
			}
		})
	}
}

// newTestPlatform creates a SlackPlatform with a nil client.
// Tests that call parseChannel before touching the Slack API are safe with nil.
func newTestPlatform() *SlackPlatform {
	return NewSlackPlatform(nil, "")
}

func TestSlackPlatform_Stream_Start_InvalidSessionID(t *testing.T) {
	_, err := newTestPlatform().Stream(context.Background(), msgiface.StreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "nocolon"},
		DeltaType:    msgiface.DeltaTypeStart,
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "invalid session ID")
}

func TestSlackPlatform_Stream_Append_InvalidSessionID(t *testing.T) {
	_, err := newTestPlatform().Stream(context.Background(), msgiface.StreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "nocolon", Text: "hi"},
		StreamID:     "stream-1",
		DeltaType:    msgiface.DeltaTypeAppend,
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "invalid session ID")
}

func TestSlackPlatform_Stream_End_InvalidSessionID(t *testing.T) {
	_, err := newTestPlatform().Stream(context.Background(), msgiface.StreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "nocolon"},
		StreamID:     "stream-1",
		DeltaType:    msgiface.DeltaTypeEnd,
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "invalid session ID")
}

func TestSlackPlatform_Stream_Append_RequiresStreamID(t *testing.T) {
	_, err := newTestPlatform().Stream(context.Background(), msgiface.StreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "slack:C12345", Text: "hi"},
		DeltaType:    msgiface.DeltaTypeAppend,
		// StreamID intentionally omitted
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "StreamID is required")
}

func TestSlackPlatform_Stream_End_RequiresStreamID(t *testing.T) {
	_, err := newTestPlatform().Stream(context.Background(), msgiface.StreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "slack:C12345"},
		DeltaType:    msgiface.DeltaTypeEnd,
		// StreamID intentionally omitted
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "StreamID is required")
}

func TestSlackPlatform_PostMessage_InvalidSessionID(t *testing.T) {
	err := newTestPlatform().PostMessage(context.Background(), msgiface.TextMetadata{
		SessionID: "nocolon",
		Text:      "hello",
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "invalid session ID")
}

func TestSlackPlatform_PostPrompt_UnknownType(t *testing.T) {
	// parseChannel succeeds, then unknown type returns error before any Slack API call.
	_, err := newTestPlatform().PostPrompt(context.Background(), PostPromptInput{
		Channel:  "slack:C12345",
		PromptID: "p1",
		Text:     "choose one",
		Type:     "unknown",
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "unknown prompt type")
}
