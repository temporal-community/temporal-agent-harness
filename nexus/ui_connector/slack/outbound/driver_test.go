package slackoutbound

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"

	slackapi "github.com/slack-go/slack"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"

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

func TestSlackPlatform_BeginStream_InvalidSessionID(t *testing.T) {
	_, err := newTestPlatform().BeginStream(context.Background(), router.BeginStreamInput{
		TextMetadata: router.TextMetadata{SessionID: "nocolon"},
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "invalid session ID")
}

func TestSlackPlatform_UpdateStream_InvalidSessionID(t *testing.T) {
	err := newTestPlatform().UpdateStream(context.Background(), router.UpdateStreamInput{
		TextMetadata: router.TextMetadata{SessionID: "nocolon", Text: "hi"},
		Handle:       router.StreamHandle{ID: "stream-1", SessionID: "nocolon"},
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "invalid session ID")
}

func TestSlackPlatform_FinishStream_InvalidSessionID(t *testing.T) {
	err := newTestPlatform().FinishStream(context.Background(), router.FinishStreamInput{
		TextMetadata: router.TextMetadata{SessionID: "nocolon"},
		Handle:       router.StreamHandle{ID: "stream-1", SessionID: "nocolon"},
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "invalid session ID")
}

func TestSlackPlatform_UpdateStream_RequiresStreamID(t *testing.T) {
	err := newTestPlatform().UpdateStream(context.Background(), router.UpdateStreamInput{
		TextMetadata: router.TextMetadata{SessionID: "slack:C12345", Text: "hi"},
		Handle:       router.StreamHandle{SessionID: "slack:C12345"},
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "stream handle ID is required")
}

func TestSlackPlatform_FinishStream_RequiresStreamID(t *testing.T) {
	err := newTestPlatform().FinishStream(context.Background(), router.FinishStreamInput{
		TextMetadata: router.TextMetadata{SessionID: "slack:C12345"},
		Handle:       router.StreamHandle{SessionID: "slack:C12345"},
	})
	require.Error(t, err)
	assert.Contains(t, err.Error(), "stream handle ID is required")
}

func TestFlattenForDisplay(t *testing.T) {
	cases := []struct {
		name  string
		input router.UpdateStreamInput
		want  string
	}{
		{"reply text", router.UpdateStreamInput{Delta: "hello"}, "hello"},
		{"tool started", router.UpdateStreamInput{ToolStatus: &router.ToolStatus{ToolName: "search", Status: router.ToolStarted}}, "\n_search..._"},
		{"tool completed", router.UpdateStreamInput{ToolStatus: &router.ToolStatus{ToolName: "search", Status: router.ToolCompleted}}, " ✅\n\n"},
		{"tool errored", router.UpdateStreamInput{ToolStatus: &router.ToolStatus{ToolName: "search", Status: router.ToolErrored, Message: "timeout"}}, " ❌ Error: timeout\n\n"},
		{"thought summary", router.UpdateStreamInput{ThoughtSummary: "thinking..."}, "thinking..."},
		{"empty", router.UpdateStreamInput{}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.want, flattenForDisplay(tc.input))
		})
	}
}

func TestFlattenSegments(t *testing.T) {
	segments := []router.Delta{
		{ToolStatus: &router.ToolStatus{ToolID: "t1", ToolName: "file_search", Status: router.ToolStarted}},
		{ToolStatus: &router.ToolStatus{ToolID: "t1", ToolName: "file_search", Status: router.ToolCompleted}},
		{Text: "A Local Activity runs in the Workflow process."},
	}
	assert.Equal(t,
		"\n_file_search..._ ✅\n\nA Local Activity runs in the Workflow process.",
		flattenSegments(segments),
	)
}

func TestRetargetEndIndex(t *testing.T) {
	segments := []router.Delta{
		{ToolStatus: &router.ToolStatus{ToolName: "search", Status: router.ToolStarted}},   // "\n_search..._"
		{ToolStatus: &router.ToolStatus{ToolName: "search", Status: router.ToolCompleted}}, // " ✅\n\n"
		{Text: "hello world"},
	}
	cases := []struct {
		name     string
		endIndex int
		want     int
	}{
		{"offset into the reply text skips past the tool-status prefix", 5, len([]rune("\n_search..._ ✅\n\n")) + 5},
		{"offset at the end of the reply text", 11, len([]rune(flattenSegments(segments)))},
		{"negative EndIndex stays negative - Splice appends at the very end", -1, -1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.want, retargetEndIndex(segments, tc.endIndex))
		})
	}
}

func TestRetargetCitations_NoToolStatus_LeavesEndIndexUnchanged(t *testing.T) {
	segments := []router.Delta{{Text: "hello world"}}
	got := retargetCitations(segments, []router.Citation{{URL: "https://example.com/doc", EndIndex: 5}})
	assert.Equal(t, []router.Citation{{URL: "https://example.com/doc", EndIndex: 5}}, got)
}

// TestSlackPlatform_UpdateStream_ToolStatusAndThoughtSummary_AppendInline verifies tool
// status and thought summaries append into the same live stream as reply text (one
// message, not a separate one per event) - the citation-position bug this used to risk
// is instead prevented by FinishStream rebuilding the final message from Segments and
// retargeting citations against the flattened result (see retargetEndIndex), regardless
// of what was shown live.
func TestSlackPlatform_UpdateStream_ToolStatusAndThoughtSummary_AppendInline(t *testing.T) {
	type request struct {
		path string
		form url.Values
	}
	var requests []request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.NoError(t, r.ParseForm())
		requests = append(requests, request{path: r.URL.Path, form: r.Form})
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "channel": "C12345", "ts": "1721609700.000000"})
	}))
	defer srv.Close()
	client := slackapi.New("test-token", slackapi.OptionAPIURL(srv.URL+"/"))
	platform := NewSlackPlatform(client, "")

	handle := router.StreamHandle{ID: "1721609600.123456", SessionID: "slack:C12345"}
	require.NoError(t, platform.UpdateStream(context.Background(), router.UpdateStreamInput{
		TextMetadata: router.TextMetadata{SessionID: "slack:C12345"},
		Handle:       handle,
		ToolStatus:   &router.ToolStatus{ToolName: "search", Status: router.ToolStarted},
	}))
	require.NoError(t, platform.UpdateStream(context.Background(), router.UpdateStreamInput{
		TextMetadata:   router.TextMetadata{SessionID: "slack:C12345"},
		Handle:         handle,
		ThoughtSummary: "thinking...",
	}))

	require.Len(t, requests, 2)
	for _, req := range requests {
		assert.Equal(t, "/chat.appendStream", req.path)
		assert.Equal(t, "1721609600.123456", req.form.Get("ts"))
	}
	assert.Equal(t, "\n_search..._", requests[0].form.Get("markdown_text"))
	assert.Equal(t, "thinking...", requests[1].form.Get("markdown_text"))
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

	handle, err := newPlatform().BeginStream(context.Background(), router.BeginStreamInput{
		TextMetadata: router.TextMetadata{
			SessionID: "slack:C12345",
			ThreadID:  "1721609500.000001",
			SenderID:  "U12345",
		},
	})
	require.NoError(t, err)
	assert.Equal(t, "1721609600.123456", handle.ID)
	assert.Equal(t, "slack:C12345", handle.SessionID)
	assert.False(t, handle.CloseBeforeApproval)

	err = newPlatform().UpdateStream(context.Background(), router.UpdateStreamInput{
		TextMetadata: router.TextMetadata{SessionID: "slack:C12345"},
		Handle:       handle,
		Delta:        "hello",
	})
	require.NoError(t, err)

	err = newPlatform().FinishStream(context.Background(), router.FinishStreamInput{
		TextMetadata: router.TextMetadata{SessionID: "slack:C12345"},
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

// TestSlackPlatform_FinishStream_NoCitations_StillCorrectsMessage verifies FinishStream
// always rebuilds the message from Segments, even with no citations.
func TestSlackPlatform_FinishStream_NoCitations_StillCorrectsMessage(t *testing.T) {
	type request struct {
		path string
		form url.Values
	}
	var requests []request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.NoError(t, r.ParseForm())
		requests = append(requests, request{path: r.URL.Path, form: r.Form})
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "channel": "C12345", "ts": "1721609600.123456"})
	}))
	defer srv.Close()
	client := slackapi.New("test-token", slackapi.OptionAPIURL(srv.URL+"/"))
	platform := NewSlackPlatform(client, "")

	err := platform.FinishStream(context.Background(), router.FinishStreamInput{
		TextMetadata: router.TextMetadata{
			SessionID: "slack:C12345",
			Text:      "hello world",
			Segments:  []router.Delta{{Text: "hello world"}},
		},
		Handle: router.StreamHandle{ID: "1721609600.123456", SessionID: "slack:C12345"},
	})
	require.NoError(t, err)

	require.Len(t, requests, 2)
	assert.Equal(t, "/chat.stopStream", requests[0].path)
	assert.Equal(t, "/chat.update", requests[1].path)
	assert.Equal(t, "hello world", requests[1].form.Get("markdown_text"))
}

// TestSlackPlatform_FinishStream_PreservesToolStatusInFinalMessage verifies tool status
// lines from Segments survive into the corrected final message, with citations
// retargeted to their position in the flattened (tool-status-included) text - this is
// the behavior that replaced the old "always rebuild from clean reply-only text" design.
func TestSlackPlatform_FinishStream_PreservesToolStatusInFinalMessage(t *testing.T) {
	type request struct {
		path string
		form url.Values
	}
	var requests []request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.NoError(t, r.ParseForm())
		requests = append(requests, request{path: r.URL.Path, form: r.Form})
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "channel": "C12345", "ts": "1721609600.123456"})
	}))
	defer srv.Close()
	client := slackapi.New("test-token", slackapi.OptionAPIURL(srv.URL+"/"))
	platform := NewSlackPlatform(client, "")

	err := platform.FinishStream(context.Background(), router.FinishStreamInput{
		TextMetadata: router.TextMetadata{
			SessionID: "slack:C12345",
			Segments: []router.Delta{
				{ToolStatus: &router.ToolStatus{ToolName: "search", Status: router.ToolStarted}},
				{ToolStatus: &router.ToolStatus{ToolName: "search", Status: router.ToolCompleted}},
				{Text: "hello world"},
			},
			Citations: []router.Citation{{URL: "https://example.com/doc", EndIndex: 5}},
		},
		Handle: router.StreamHandle{ID: "1721609600.123456", SessionID: "slack:C12345"},
	})
	require.NoError(t, err)

	require.Len(t, requests, 2)
	assert.Equal(t, "/chat.update", requests[1].path)
	assert.Equal(t, "\n_search..._ ✅\n\nhello [[1]](https://example.com/doc) world", requests[1].form.Get("markdown_text"))
}

// TestSlackPlatform_FinishStream_EmptyText_SkipsCorrection guards the edge case where
// there's nothing to correct to (e.g. a turn that ended with no reply text at all) -
// chat.update with empty markdown_text would otherwise error.
func TestSlackPlatform_FinishStream_EmptyText_SkipsCorrection(t *testing.T) {
	type request struct{ path string }
	var requests []request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests = append(requests, request{path: r.URL.Path})
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "channel": "C12345", "ts": "1721609600.123456"})
	}))
	defer srv.Close()
	client := slackapi.New("test-token", slackapi.OptionAPIURL(srv.URL+"/"))
	platform := NewSlackPlatform(client, "")

	err := platform.FinishStream(context.Background(), router.FinishStreamInput{
		TextMetadata: router.TextMetadata{SessionID: "slack:C12345"},
		Handle:       router.StreamHandle{ID: "1721609600.123456", SessionID: "slack:C12345"},
	})
	require.NoError(t, err)

	require.Len(t, requests, 1)
	assert.Equal(t, "/chat.stopStream", requests[0].path)
}

func TestSlackPlatform_FinishStream_WithCitations_CorrectsMessage(t *testing.T) {
	type request struct {
		path string
		form url.Values
	}
	var requests []request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.NoError(t, r.ParseForm())
		requests = append(requests, request{path: r.URL.Path, form: r.Form})
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "channel": "C12345", "ts": "1721609600.123456"})
	}))
	defer srv.Close()
	client := slackapi.New("test-token", slackapi.OptionAPIURL(srv.URL+"/"))
	platform := NewSlackPlatform(client, "")

	err := platform.FinishStream(context.Background(), router.FinishStreamInput{
		TextMetadata: router.TextMetadata{
			SessionID: "slack:C12345",
			Text:      "hello world",
			Segments:  []router.Delta{{Text: "hello world"}},
			Citations: []router.Citation{{URL: "https://example.com/doc", EndIndex: 5}},
		},
		Handle: router.StreamHandle{ID: "1721609600.123456", SessionID: "slack:C12345"},
	})
	require.NoError(t, err)

	require.Len(t, requests, 2)
	assert.Equal(t, "/chat.stopStream", requests[0].path)
	assert.Equal(t, "/chat.update", requests[1].path)
	assert.Equal(t, "1721609600.123456", requests[1].form.Get("ts"))
	assert.Equal(t, "hello [[1]](https://example.com/doc) world", requests[1].form.Get("markdown_text"))
}

func TestSlackPlatform_PostMessage_InvalidSessionID(t *testing.T) {
	err := newTestPlatform().PostMessage(context.Background(), router.TextMetadata{
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
