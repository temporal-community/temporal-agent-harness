package teams

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	msgiface "github.com/temporal-community/temporal-agent-harness/nexus/slack_connector/messaging"

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
	type request struct {
		method string
		path   string
		body   msgiface.TeamMessageActivity
	}
	var requests []request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body msgiface.TeamMessageActivity
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		requests = append(requests, request{method: r.Method, path: r.URL.Path, body: body})
		_, _ = w.Write([]byte(`{"id":"stream-1"}`))
	}))
	defer srv.Close()

	ctx := context.Background()

	// Use a fresh adapter instance for every phase to prove the lifecycle does
	// not depend on worker-local memory.
	handle, err := NewTeamsPlatform(newTestBot(), srv.URL).BeginStream(ctx, msgiface.BeginStreamInput{
		TextMetadata:     msgiface.TextMetadata{SessionID: "teams:conv-1", Text: "Hello"},
		ConversationType: "personal",
	})
	require.NoError(t, err)
	require.Equal(t, "stream-1", handle.ID)
	assert.Equal(t, streamModeNative, handle.TransportMode)
	assert.Equal(t, msgiface.StreamWireTextFullText, handle.WireTextMode)
	assert.Equal(t, initialStreamSequence+1, handle.NextSequence)

	// The workflow supplies both the latest delta and the cumulative full text;
	// the Teams REST adapter sends the cumulative representation.
	err = NewTeamsPlatform(newTestBot(), srv.URL).UpdateStream(ctx, msgiface.UpdateStreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "teams:conv-1"},
		Handle:       handle,
		Delta:        " world",
		FullText:     "Hello world",
		Sequence:     handle.NextSequence,
	})
	require.NoError(t, err)

	err = NewTeamsPlatform(newTestBot(), srv.URL).FinishStream(ctx, msgiface.FinishStreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "teams:conv-1"},
		Handle:       handle,
		FullText:     "Hello world",
	})
	require.NoError(t, err)

	require.Len(t, requests, 3)
	assert.Equal(t, http.MethodPost, requests[0].method)
	assert.Equal(t, "/v3/conversations/conv-1/activities", requests[0].path)
	assert.Equal(t, activityTypeTyping, requests[0].body.Type)
	require.Len(t, requests[0].body.Entities, 1)
	assert.Equal(t, streamTypeInformative, requests[0].body.Entities[0].StreamType)

	assert.Equal(t, http.MethodPost, requests[1].method)
	assert.Equal(t, "Hello world", requests[1].body.Text)
	assert.Equal(t, activityTypeTyping, requests[1].body.Type)
	require.Len(t, requests[1].body.Entities, 1)
	assert.Equal(t, streamTypeStreaming, requests[1].body.Entities[0].StreamType)
	require.NotNil(t, requests[1].body.Entities[0].StreamSequence)
	assert.Equal(t, initialStreamSequence+1, *requests[1].body.Entities[0].StreamSequence)

	assert.Equal(t, http.MethodPost, requests[2].method)
	assert.Equal(t, "Hello world", requests[2].body.Text)
	assert.Equal(t, activityTypeMessage, requests[2].body.Type)
	require.Len(t, requests[2].body.Entities, 1)
	assert.Equal(t, streamTypeFinal, requests[2].body.Entities[0].StreamType)
	assert.Nil(t, requests[2].body.Entities[0].StreamSequence)
}

func TestStreamChannelAndGroupChatUseMessageUpdates(t *testing.T) {
	for _, conversationType := range []string{"channel", "groupChat"} {
		t.Run(conversationType, func(t *testing.T) {
			type request struct {
				method string
				path   string
				body   msgiface.TeamMessageActivity
			}
			var requests []request
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body msgiface.TeamMessageActivity
				raw, _ := io.ReadAll(r.Body)
				require.NoError(t, json.Unmarshal(raw, &body))
				requests = append(requests, request{method: r.Method, path: r.URL.Path, body: body})
				_, _ = w.Write([]byte(`{"id":"message-1"}`))
			}))
			defer srv.Close()

			p := NewTeamsPlatform(newTestBot(), srv.URL)
			ctx := context.Background()
			handle, err := p.BeginStream(ctx, msgiface.BeginStreamInput{
				TextMetadata: msgiface.TextMetadata{
					SessionID: "teams:conv-1",
					ThreadID:  "question-1",
				},
				ConversationType: conversationType,
			})
			require.NoError(t, err)
			assert.Equal(t, "message-1", handle.ID)
			assert.Equal(t, streamModeMessageUpdate, handle.TransportMode)

			err = p.UpdateStream(ctx, msgiface.UpdateStreamInput{
				TextMetadata: msgiface.TextMetadata{SessionID: "teams:conv-1"},
				Handle:       handle,
				Delta:        "Hello",
				FullText:     "Hello",
			})
			require.NoError(t, err)
			err = p.UpdateStream(ctx, msgiface.UpdateStreamInput{
				TextMetadata: msgiface.TextMetadata{SessionID: "teams:conv-1"},
				Handle:       handle,
				Delta:        " world",
				FullText:     "Hello world",
			})
			require.NoError(t, err)
			err = p.FinishStream(ctx, msgiface.FinishStreamInput{
				TextMetadata: msgiface.TextMetadata{SessionID: "teams:conv-1"},
				Handle:       handle,
				FullText:     "Hello world",
			})
			require.NoError(t, err)

			require.Len(t, requests, 4)
			assert.Equal(t, http.MethodPost, requests[0].method)
			assert.Equal(t, "/v3/conversations/conv-1/activities/question-1", requests[0].path)
			assert.Equal(t, activityTypeMessage, requests[0].body.Type)
			assert.Equal(t, initialStreamingText, requests[0].body.Text)
			assert.Empty(t, requests[0].body.Entities)

			for _, update := range requests[1:] {
				assert.Equal(t, http.MethodPut, update.method)
				assert.Equal(t, "/v3/conversations/conv-1/activities/message-1", update.path)
				assert.Equal(t, activityTypeMessage, update.body.Type)
				assert.Empty(t, update.body.Entities)
			}
			assert.Equal(t, "Hello", requests[1].body.Text)
			assert.Equal(t, "Hello world", requests[2].body.Text)
			assert.Equal(t, "Hello world", requests[3].body.Text)
		})
	}
}

func TestStreamUnknownConversationFallsBackOnStreaming405(t *testing.T) {
	type request struct {
		method string
		path   string
		body   msgiface.TeamMessageActivity
	}
	var requests []request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body msgiface.TeamMessageActivity
		raw, _ := io.ReadAll(r.Body)
		require.NoError(t, json.Unmarshal(raw, &body))
		requests = append(requests, request{method: r.Method, path: r.URL.Path, body: body})
		switch len(requests) {
		case 1:
			http.Error(w, "Streaming is not enabled for non personal scope", http.StatusMethodNotAllowed)
		case 2:
			_, _ = w.Write([]byte(`{"id":"message-1"}`))
		default:
			_, _ = w.Write([]byte(`{"id":"message-1"}`))
		}
	}))
	defer srv.Close()

	p := NewTeamsPlatform(newTestBot(), srv.URL)
	handle, err := p.BeginStream(context.Background(), msgiface.BeginStreamInput{
		TextMetadata: msgiface.TextMetadata{
			SessionID: "teams:conv-1",
			ThreadID:  "question-1",
		},
	})
	require.NoError(t, err)
	assert.Equal(t, "message-1", handle.ID)
	assert.Equal(t, streamModeMessageUpdate, handle.TransportMode)

	err = p.UpdateStream(context.Background(), msgiface.UpdateStreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "teams:conv-1"},
		Handle:       handle,
		Delta:        "answer",
		FullText:     "answer",
	})
	require.NoError(t, err)
	err = p.FinishStream(context.Background(), msgiface.FinishStreamInput{
		TextMetadata: msgiface.TextMetadata{SessionID: "teams:conv-1"},
		Handle:       handle,
		FullText:     "answer",
	})
	require.NoError(t, err)

	require.Len(t, requests, 4)
	assert.Equal(t, http.MethodPost, requests[0].method)
	assert.Equal(t, "/v3/conversations/conv-1/activities", requests[0].path)
	assert.Equal(t, activityTypeTyping, requests[0].body.Type)
	assert.NotEmpty(t, requests[0].body.Entities)
	assert.Equal(t, http.MethodPost, requests[1].method)
	assert.Equal(t, "/v3/conversations/conv-1/activities/question-1", requests[1].path)
	assert.Equal(t, activityTypeMessage, requests[1].body.Type)
	assert.Empty(t, requests[1].body.Entities)
	for _, update := range requests[2:] {
		assert.Equal(t, http.MethodPut, update.method)
		assert.Equal(t, "/v3/conversations/conv-1/activities/message-1", update.path)
		assert.Equal(t, "answer", update.body.Text)
	}
}

func TestPostMessageDoesNotRetryRateLimit(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		assert.Equal(t, http.MethodPost, r.Method)
		assert.Equal(t, "/v3/conversations/conv-1/activities/question-1", r.URL.Path)
		http.Error(w, "rate limited", http.StatusTooManyRequests)
	}))
	defer srv.Close()

	p := NewTeamsPlatform(newTestBot(), srv.URL)
	err := p.PostMessage(context.Background(), msgiface.TextMetadata{
		SessionID: "teams:conv-1",
		ThreadID:  "question-1",
		Text:      "answer",
	})
	require.Error(t, err)
	assert.Equal(t, 1, calls)
	var httpErr *teamsHTTPError
	require.ErrorAs(t, err, &httpErr)
	assert.Equal(t, http.StatusTooManyRequests, httpErr.StatusCode)
	assert.Equal(t, "rate limited", httpErr.Body)
}
