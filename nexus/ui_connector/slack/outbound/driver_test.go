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

func TestSpliceCitations(t *testing.T) {
	cases := []struct {
		name      string
		text      string
		citations []router.Citation
		link      func(url, title string) string
		want      string
	}{
		{"no citations", "hello world", nil, mrkdwnLink, "hello world"},
		{
			"mrkdwn marker inserted at EndIndex",
			"hello world",
			[]router.Citation{{URL: "https://example.com/doc", Title: "Doc", EndIndex: 5}},
			mrkdwnLink,
			"hello <https://example.com/doc|[1]> world",
		},
		{
			"commonmark marker inserted at EndIndex",
			"hello world",
			[]router.Citation{{URL: "https://example.com/doc", Title: "Doc", EndIndex: 5}},
			commonmarkLink,
			"hello [[1]](https://example.com/doc) world",
		},
		{
			"negative EndIndex appends at the end",
			"hello world",
			[]router.Citation{{URL: "https://example.com/doc", EndIndex: -1}},
			mrkdwnLink,
			"hello world <https://example.com/doc|[1]>",
		},
		{
			"EndIndex past the end of the text clamps to the end",
			"hello",
			[]router.Citation{{URL: "https://example.com/doc", EndIndex: 999}},
			mrkdwnLink,
			"hello <https://example.com/doc|[1]>",
		},
		{
			"citations at the same index coalesce adjacent, numbered by array order",
			"hello world",
			[]router.Citation{
				{URL: "https://example.com/a", EndIndex: 5},
				{URL: "https://example.com/b", EndIndex: 5},
			},
			mrkdwnLink,
			"hello <https://example.com/a|[1]> <https://example.com/b|[2]> world",
		},
		{
			"repeated source is not deduped - gets a new number each time",
			"a b",
			[]router.Citation{
				{URL: "https://example.com/doc", EndIndex: 1},
				{URL: "https://example.com/doc", EndIndex: 3},
			},
			mrkdwnLink,
			"a <https://example.com/doc|[1]> b <https://example.com/doc|[2]>",
		},
		{
			// EndIndex 14 lands between "st" and "ep" of "step" (index 12-15). The
			// marker must snap past the rest of the word rather than splitting it.
			"EndIndex mid-word snaps to the end of the word",
			"downscaling step regardless",
			[]router.Citation{{URL: "https://example.com/doc", EndIndex: 14}},
			mrkdwnLink,
			"downscaling step <https://example.com/doc|[1]> regardless",
		},
		{
			// EndIndex 6 lands exactly on the existing space between "these" and
			// "operations" - the result must have exactly one space on each side of
			// the marker, not the original spacing preserved verbatim.
			"marker gets exactly one space on each side regardless of existing spacing",
			"these operations",
			[]router.Citation{{URL: "https://example.com/doc", EndIndex: 6}},
			mrkdwnLink,
			"these <https://example.com/doc|[1]> operations",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.want, spliceCitations(tc.text, tc.citations, tc.link))
		})
	}
}

func TestSnapToWordEnd(t *testing.T) {
	cases := []struct {
		name string
		text string
		idx  int
		want int
	}{
		{"mid-word snaps to end of word", "step", 2, 4},
		{"already at a word boundary (space) is unchanged", "a b", 1, 1},
		{"at the very start is unchanged", "step", 0, 0},
		{"at the very end is unchanged", "step", 4, 4},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.want, snapToWordEnd([]rune(tc.text), tc.idx))
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

func TestSlackPlatform_FinishStream_NoCitations_DoesNotCorrectMessage(t *testing.T) {
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
		TextMetadata: router.TextMetadata{SessionID: "slack:C12345", Text: "hello world"},
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
