package slack

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"

	slackapi "github.com/slack-go/slack"
	msgiface "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound"

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
		{"", "", true},
		{"nocolon", "", true},
		{"slack:", "", true},
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

func TestSlackPlatform_BeginStream_InvalidSessionID(t *testing.T) {
	_, err := newTestPlatform().BeginStream(context.Background(), msgiface.BeginStreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "nocolon"},
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "provider:id")
}

func TestSlackPlatform_UpdateStream_InvalidSessionID(t *testing.T) {
	err := newTestPlatform().UpdateStream(context.Background(), msgiface.UpdateStreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "nocolon", Text: "hi"},
		Handle:       msgiface.StreamHandle{ID: "stream-1", SessionID: "nocolon"},
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "provider:id")
}

func TestSlackPlatform_FinishStream_InvalidSessionID(t *testing.T) {
	err := newTestPlatform().FinishStream(context.Background(), msgiface.FinishStreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "nocolon"},
		Handle:       msgiface.StreamHandle{ID: "stream-1", SessionID: "nocolon"},
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "provider:id")
}

func TestSlackPlatform_UpdateStream_RequiresStreamID(t *testing.T) {
	err := newTestPlatform().UpdateStream(context.Background(), msgiface.UpdateStreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "slack:C12345", Text: "hi"},
		Handle:       msgiface.StreamHandle{SessionID: "slack:C12345"},
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "stream handle ID is required")
}

func TestSlackPlatform_FinishStream_RequiresStreamID(t *testing.T) {
	err := newTestPlatform().FinishStream(context.Background(), msgiface.FinishStreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "slack:C12345"},
		Handle:       msgiface.StreamHandle{SessionID: "slack:C12345"},
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "stream handle ID is required")
}

func TestSlackPlatform_StatelessStreamLifecycle(t *testing.T) {
	type request struct {
		path string
		form url.Values
	}
	var requests []request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.NoError(t, r.ParseForm())
		requests = append(requests, request{path: r.URL.Path, form: r.Form})
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"ok":      true,
			"channel": "C12345",
			"ts":      "1721609600.123456",
		})
	}))
	defer srv.Close()

	newPlatform := func() *SlackPlatform {
		client := slackapi.New("test-token", slackapi.OptionAPIURL(srv.URL+"/"))
		return NewSlackPlatform(client, "T12345")
	}

	handle, err := newPlatform().BeginStream(context.Background(), msgiface.BeginStreamInput{
		TextMetadata: msgiface.TextMetadata{
			SessionID: "slack:C12345",
			ThreadID:  "1721609500.000001",
			SenderID:  "U12345",
		},
	})
	require.NoError(t, err)
	assert.Equal(t, "1721609600.123456", handle.ID)
	assert.Equal(t, "slack:C12345", handle.SessionID)
	assert.False(t, handle.CloseBeforeApproval)

	err = newPlatform().UpdateStream(context.Background(), msgiface.UpdateStreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "slack:C12345"},
		Handle:       handle,
		Delta:        "hello",
	})
	require.NoError(t, err)

	err = newPlatform().FinishStream(context.Background(), msgiface.FinishStreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "slack:C12345"},
		Handle:       handle,
	})
	require.NoError(t, err)

	require.Len(t, requests, 3)
	assert.Equal(t, "/chat.startStream", requests[0].path)
	assert.Equal(t, "C12345", requests[0].form.Get("channel"))
	assert.Equal(t, "1721609500.000001", requests[0].form.Get("thread_ts"))
	assert.Equal(t, "U12345", requests[0].form.Get("recipient_user_id"))
	assert.Equal(t, "T12345", requests[0].form.Get("recipient_team_id"))

	assert.Equal(t, "/chat.appendStream", requests[1].path)
	assert.Equal(t, "1721609600.123456", requests[1].form.Get("ts"))
	assert.Equal(t, "hello", requests[1].form.Get("markdown_text"))

	assert.Equal(t, "/chat.stopStream", requests[2].path)
	assert.Equal(t, "1721609600.123456", requests[2].form.Get("ts"))
}

func TestSlackPlatform_PostMessage_InvalidSessionID(t *testing.T) {
	err := newTestPlatform().PostMessage(context.Background(), msgiface.TextMetadata{
		SessionID: "nocolon",
		Text:      "hello",
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "provider:id")
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
