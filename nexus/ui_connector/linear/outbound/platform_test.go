package linearoutbound

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

type activityRequest struct {
	SessionID string          `json:"agentSessionId"`
	Content   activityContent `json:"content"`
	Ephemeral bool            `json:"ephemeral"`
}

type fakeLinear struct {
	server   *httptest.Server
	requests []activityRequest
	status   int
	response string
}

func newFakeLinear(t *testing.T) (*fakeLinear, *LinearPlatform) {
	t.Helper()
	f := &fakeLinear{status: http.StatusOK, response: `{"data":{"agentActivityCreate":{"success":true}}}`}
	f.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		assert.Equal(t, http.MethodPost, r.Method)
		if r.URL.Path == "/oauth/token" {
			_, _ = w.Write([]byte(`{"access_token":"test-token","token_type":"Bearer","expires_in":2592000}`))
			return
		}
		assert.Equal(t, "application/json", r.Header.Get("Content-Type"))
		assert.Equal(t, "Bearer test-token", r.Header.Get("Authorization"))
		var request struct {
			Query     string `json:"query"`
			Variables struct {
				Input activityRequest `json:"input"`
			} `json:"variables"`
		}
		if !assert.NoError(t, json.NewDecoder(r.Body).Decode(&request)) {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		assert.Contains(t, request.Query, "agentActivityCreate(input: $input)")
		f.requests = append(f.requests, request.Variables.Input)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(f.status)
		_, _ = w.Write([]byte(f.response))
	}))
	t.Cleanup(f.server.Close)
	c := NewClient("client-id", "client-secret")
	c.TokenEndpoint = f.server.URL + "/oauth/token"
	c.Endpoint = f.server.URL
	return f, NewLinearPlatform(c)
}

var address = json.RawMessage(`{"sessionId":"linear:org-1:session-1","agentSessionId":"session-1","organizationId":"org-1"}`)

func text(value string) protocol.Event {
	return protocol.Event{Chunk: &protocol.Chunk{Text: value}}
}

func deliver(t *testing.T, p *LinearPlatform, state json.RawMessage, end bool, events ...protocol.Event) json.RawMessage {
	t.Helper()
	next, err := p.Deliver(context.Background(), DeliverInput{Address: address, State: state, Events: events, End: end})
	require.NoError(t, err)
	return next
}

func TestReplyIsBufferedAcrossBatchesAndPostedOnDone(t *testing.T) {
	f, p := newFakeLinear(t)
	state := deliver(t, p, nil, false, protocol.Event{TurnStarted: true}, text("Hello "))
	require.Equal(t, []activityRequest{{SessionID: "session-1", Content: activityContent{Type: "thought", Body: "Working on your request."}, Ephemeral: true}}, f.requests)
	state = deliver(t, p, state, false, protocol.Event{TurnStarted: true}, text("**world**"))
	assert.Len(t, f.requests, 1, "no streamed text and no repeated acknowledgement")
	assert.NotContains(t, string(state), "test-token")
	assert.Nil(t, deliver(t, p, state, true))
	require.Len(t, f.requests, 2)
	assert.Equal(t, activityRequest{SessionID: "session-1", Content: activityContent{Type: "response", Body: "Hello **world**"}}, f.requests[1])
}

func TestCitationsAndIgnoredProgress(t *testing.T) {
	f, p := newFakeLinear(t)
	state := deliver(t, p, nil, false,
		text("Hello 🌎"),
		protocol.Event{Chunk: &protocol.Chunk{Tool: &protocol.ToolChunk{Name: "private-tool", Output: "private output"}}},
		protocol.Event{Chunk: &protocol.Chunk{Thought: &protocol.ThoughtChunk{Delta: "private thought"}}},
		protocol.Event{Usage: &protocol.ModelUsage{Model: "private-model"}},
	)
	state = deliver(t, p, state, false,
		protocol.Event{Chunk: &protocol.Chunk{Citations: []protocol.Citation{{URL: "https://example.com", EndIndex: 7}}}},
		text("!"),
	)
	deliver(t, p, state, true)
	require.Len(t, f.requests, 1)
	assert.Equal(t, "Hello 🌎! [[1]](https://example.com)", f.requests[0].Content.Body)
}

func TestHandlerOutputFallback(t *testing.T) {
	for _, tc := range []struct {
		name   string
		output map[string]any
		text   string
		want   string
	}{
		{"text field", map[string]any{"text": "Answer"}, "", "Answer"},
		{"structured", map[string]any{"count": float64(3)}, "", "```json\n{\n  \"count\": 3\n}\n```"},
		{"empty object", map[string]any{}, "", "_Done._"},
		{"reply wins", map[string]any{"text": "duplicate"}, "Real answer", "Real answer"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f, p := newFakeLinear(t)
			state := deliver(t, p, nil, false, text(tc.text))
			state = deliver(t, p, state, false, protocol.Event{HandlerOutput: &protocol.HandlerOutput{Value: tc.output}})
			deliver(t, p, state, true)
			require.Len(t, f.requests, 1)
			assert.Equal(t, tc.want, f.requests[0].Content.Body)
		})
	}
}

func TestErrorReplacesPartialReply(t *testing.T) {
	f, p := newFakeLinear(t)
	state := deliver(t, p, nil, false, text("incomplete answer"))
	state = deliver(t, p, state, false, protocol.Event{Error: "The agent's handler failed"})
	assert.Empty(t, f.requests)
	deliver(t, p, state, true)
	require.Len(t, f.requests, 1)
	assert.Equal(t, activityContent{Type: "error", Body: "The agent's handler failed"}, f.requests[0].Content)
}

func TestCommandHelpAndNotices(t *testing.T) {
	f, p := newFakeLinear(t)
	handler := protocol.Handler{Name: "ask", Description: "Ask a question"}
	state := deliver(t, p, nil, false,
		protocol.Event{Notice: "The agent is busy"},
		protocol.Event{Guide: &protocol.Guide{Handlers: []protocol.Handler{handler}}},
		protocol.Event{Form: &handler},
		protocol.Event{HandlerHelp: &protocol.HandlerHelp{Handler: handler}},
	)
	deliver(t, p, state, true)
	require.Len(t, f.requests, 1)
	assert.Contains(t, f.requests[0].Content.Body, "ℹ️ **System message:** The agent is busy")
	assert.Contains(t, f.requests[0].Content.Body, "Ask a question")
	assert.Contains(t, f.requests[0].Content.Body, "`/ask` takes arguments")
}

func TestNoOutputDoesNotPostAnEmptyResponse(t *testing.T) {
	f, p := newFakeLinear(t)
	for _, state := range []json.RawMessage{nil, json.RawMessage("null"), json.RawMessage("{}")} {
		deliver(t, p, state, true)
	}
	assert.Empty(t, f.requests)
}

func TestEmptyCompletedTurnClearsWorkingState(t *testing.T) {
	f, p := newFakeLinear(t)
	state := deliver(t, p, nil, false, protocol.Event{TurnStarted: true})
	deliver(t, p, state, true)
	require.Len(t, f.requests, 2)
	assert.Equal(t, activityContent{Type: "response", Body: "_Done._"}, f.requests[1].Content)
}

func TestFailedAcknowledgementDoesNotLoseReply(t *testing.T) {
	f, p := newFakeLinear(t)
	f.status = http.StatusServiceUnavailable
	state := deliver(t, p, nil, false, protocol.Event{TurnStarted: true}, text("answer"))
	f.status = http.StatusOK
	deliver(t, p, state, true)
	require.Len(t, f.requests, 2)
	assert.Equal(t, activityContent{Type: "response", Body: "answer"}, f.requests[1].Content)
}

func TestInvalidAddressAndState(t *testing.T) {
	for _, tc := range []struct {
		name    string
		address json.RawMessage
		state   json.RawMessage
		want    string
	}{
		{"malformed address", json.RawMessage("{"), nil, "decode address"},
		{"null address", json.RawMessage("null"), nil, "IDs are required"},
		{"missing session", json.RawMessage(`{"organizationId":"org-1"}`), nil, "IDs are required"},
		{"missing organization", json.RawMessage(`{"agentSessionId":"session-1"}`), nil, "IDs are required"},
		{"bad state", address, json.RawMessage("{"), "decode state"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f, p := newFakeLinear(t)
			_, err := p.Deliver(context.Background(), DeliverInput{Address: tc.address, State: tc.state, End: true})
			require.ErrorContains(t, err, tc.want)
			assert.Empty(t, f.requests)
		})
	}
}

func TestAPIFailures(t *testing.T) {
	for _, tc := range []struct {
		name     string
		status   int
		response string
		want     string
	}{
		{"unauthorized", 401, "private details", "HTTP 401"},
		{"rate limited", 429, "private details", "HTTP 429"},
		{"server error", 503, "private details", "HTTP 503"},
		{"invalid JSON", 200, "private details", "invalid Linear response"},
		{"GraphQL error", 200, `{"errors":[{"message":"private details"}]}`, "agentActivityCreate failed"},
		{"partial failure", 200, `{"data":{"agentActivityCreate":{"success":true}},"errors":[{"message":"private details"}]}`, "agentActivityCreate failed"},
		{"unsuccessful", 200, `{"data":{"agentActivityCreate":{"success":false}}}`, "agentActivityCreate failed"},
		{"missing data", 200, `{"data":null}`, "agentActivityCreate failed"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f, p := newFakeLinear(t)
			f.status, f.response = tc.status, tc.response
			_, err := p.Deliver(context.Background(), DeliverInput{Address: address, Events: []protocol.Event{text("answer")}, End: true})
			require.ErrorContains(t, err, tc.want)
			assert.NotContains(t, err.Error(), "private details")
			wantCalls := 1
			if tc.status == http.StatusUnauthorized {
				wantCalls = 2
			}
			assert.Len(t, f.requests, wantCalls)
		})
	}
}

func TestRequestCancellationAndTransportFailure(t *testing.T) {
	f, p := newFakeLinear(t)
	_, err := p.client.accessToken(context.Background())
	require.NoError(t, err)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err = p.Deliver(ctx, DeliverInput{Address: address, Events: []protocol.Event{text("answer")}, End: true})
	require.ErrorIs(t, err, context.Canceled)
	assert.Empty(t, f.requests)
	f.server.Close()
	_, err = p.Deliver(context.Background(), DeliverInput{Address: address, Events: []protocol.Event{text("answer")}, End: true})
	require.ErrorContains(t, err, "HTTP request failed")
}

func TestRedirectDoesNotForwardToken(t *testing.T) {
	f, p := newFakeLinear(t)
	_, err := p.client.accessToken(context.Background())
	require.NoError(t, err)
	f.status, f.response = http.StatusTemporaryRedirect, ""
	// Use another server to ensure a future client change cannot follow redirects.
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, f.server.URL, http.StatusTemporaryRedirect)
	}))
	defer redirect.Close()
	p.client.Endpoint = redirect.URL
	_, err = p.Deliver(context.Background(), DeliverInput{Address: address, Events: []protocol.Event{text("answer")}, End: true})
	require.ErrorContains(t, err, "HTTP 307")
	assert.Empty(t, f.requests)
}

func TestFailedFinalDeliveryKeepsSavedStateUsable(t *testing.T) {
	f, p := newFakeLinear(t)
	state := deliver(t, p, nil, false, text("answer"))
	f.status = http.StatusServiceUnavailable
	_, err := p.Deliver(context.Background(), DeliverInput{Address: address, State: state, End: true})
	require.Error(t, err)
	f.status = http.StatusOK
	deliver(t, p, state, true)
	require.Len(t, f.requests, 2)
	assert.Equal(t, f.requests[0], f.requests[1])
	assert.Equal(t, "answer", f.requests[1].Content.Body)
	assert.False(t, strings.Contains(string(state), "test-token"))
}
