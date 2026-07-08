package teams

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	msgiface "github.com/temporalio/nexus_connector_slack/messaging"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestParseConversation(t *testing.T) {
	cases := []struct {
		input   string
		want    string
		wantErr bool
	}{
		{"teams:19:abc@thread.tacv2", "19:abc@thread.tacv2", false},
		{"teams:a1b2c3", "a1b2c3", false},
		{"", "", true},
		{"nocolon", "", true},
		{"teams:", "", true},
		{"slack:C12345", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.input, func(t *testing.T) {
			got, err := parseConversation(tc.input)
			if tc.wantErr {
				require.Error(t, err)
			} else {
				require.NoError(t, err)
				assert.Equal(t, tc.want, got)
			}
		})
	}
}

// newTestBot returns a TeamsBot with a pre-cached token so tests never hit
// the real OAuth endpoint.
func newTestBot() *TeamsBot {
	return &TeamsBot{
		Client:      http.DefaultClient,
		AppID:       "test-app-id",
		accessToken: "test-token",
		expiresAt:   time.Now().Add(time.Hour),
	}
}

func TestPostApprovalPrompt(t *testing.T) {
	var gotURL string
	var gotAuth string
	var gotBody msgiface.TeamMessageActivity
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotURL = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &gotBody)
		_, _ = w.Write([]byte(`{"id":"card-1"}`))
	}))
	defer srv.Close()

	p := NewTeamsPlatform(newTestBot(), srv.URL)
	err := p.PostApprovalPrompt(context.Background(), msgiface.ApprovalPromptInput{
		SessionID: "teams:conv-1",
		ToolID:    "tool-42",
		ToolName:  "search_web",
		ToolInput: `{"query":"temporal"}`,
	})
	require.NoError(t, err)

	assert.Equal(t, "/v3/conversations/conv-1/activities", gotURL)
	assert.Equal(t, "Bearer test-token", gotAuth)
	require.Len(t, gotBody.Attachments, 1)
	assert.Equal(t, adaptiveCardContentType, gotBody.Attachments[0].ContentType)

	// The card must carry decodable approve/deny button values.
	var card struct {
		Actions []struct {
			Title string              `json:"title"`
			Data  ApprovalButtonValue `json:"data"`
		} `json:"actions"`
	}
	require.NoError(t, json.Unmarshal(gotBody.Attachments[0].Content, &card))
	require.Len(t, card.Actions, 2)

	approve, deny := card.Actions[0].Data, card.Actions[1].Data
	assert.True(t, approve.Approved)
	assert.False(t, deny.Approved)
	for _, d := range []ApprovalButtonValue{approve, deny} {
		assert.Equal(t, "teams:conv-1", d.SessionID)
		assert.Equal(t, "tool-42", d.ToolID)
		assert.Equal(t, "search_web", d.ToolName)
	}
}

func TestUpdateActivity(t *testing.T) {
	var gotMethod, gotURL, gotText string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotURL = r.URL.Path
		var body msgiface.TeamMessageActivity
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		gotText = body.Text
		_, _ = w.Write([]byte(`{"id":"card-1"}`))
	}))
	defer srv.Close()

	p := NewTeamsPlatform(newTestBot(), srv.URL)
	err := p.UpdateActivity(context.Background(), "teams:conv-1", "card-1", "resolved")
	require.NoError(t, err)

	assert.Equal(t, http.MethodPut, gotMethod)
	assert.Equal(t, "/v3/conversations/conv-1/activities/card-1", gotURL)
	assert.Equal(t, "resolved", gotText)
}

func TestUpdateActivityRequiresActivityID(t *testing.T) {
	p := NewTeamsPlatform(newTestBot(), "https://example.invalid")
	err := p.UpdateActivity(context.Background(), "teams:conv-1", "", "resolved")
	require.Error(t, err)
}

func TestStreamLifecycle(t *testing.T) {
	var texts []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body msgiface.TeamMessageActivity
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		texts = append(texts, body.Text)
		_, _ = w.Write([]byte(`{"id":"stream-1"}`))
	}))
	defer srv.Close()

	p := NewTeamsPlatform(newTestBot(), srv.URL)
	ctx := context.Background()

	streamID, err := p.Stream(ctx, msgiface.StreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "teams:conv-1", Text: "Hello"},
		DeltaType:    msgiface.DeltaTypeStart,
	})
	require.NoError(t, err)
	require.Equal(t, "stream-1", streamID)

	// Appends within the throttle window are buffered, not sent.
	_, err = p.Stream(ctx, msgiface.StreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "teams:conv-1", Text: " world"},
		StreamID:     streamID,
		DeltaType:    msgiface.DeltaTypeAppend,
	})
	require.NoError(t, err)

	// End flushes the full accumulated text as a final message.
	_, err = p.Stream(ctx, msgiface.StreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "teams:conv-1"},
		StreamID:     streamID,
		DeltaType:    msgiface.DeltaTypeEnd,
	})
	require.NoError(t, err)

	require.NotEmpty(t, texts)
	assert.Equal(t, "Hello world", texts[len(texts)-1])
}
