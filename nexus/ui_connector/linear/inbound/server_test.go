package linearinbound

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/mock"
	"github.com/stretchr/testify/require"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/linear"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/mocks"
)

const testSigningSecret = "linear-signing-secret"

func createdEvent() map[string]any {
	return map[string]any{
		"type": "AgentSessionEvent", "action": "created", "organizationId": "org-1",
		"webhookTimestamp": time.Now().UnixMilli(), "promptContext": "Review this issue",
		"agentSession": map[string]any{
			"id": "session-1", "organizationId": "org-1", "creatorId": "user-1",
			"comment": map[string]any{"id": "comment-1", "body": "Root comment"},
		},
	}
}

func promptedEvent() map[string]any {
	event := createdEvent()
	event["action"] = "prompted"
	event["agentActivity"] = map[string]any{
		"id": "activity-1", "agentSessionId": "session-1", "userId": "user-2",
		"content": map[string]any{"type": "prompt", "body": "What about the tests?"},
	}
	return event
}

func signedRequest(body, secret string) *http.Request {
	r := httptest.NewRequest(http.MethodPost, routeEvents, strings.NewReader(body))
	r.Header.Set("Content-Type", "application/json")
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write([]byte(body))
	r.Header.Set("Linear-Signature", hex.EncodeToString(mac.Sum(nil)))
	return r
}

func postEvent(t *testing.T, server http.Handler, event map[string]any) *httptest.ResponseRecorder {
	t.Helper()
	body, err := json.Marshal(event)
	require.NoError(t, err)
	response := httptest.NewRecorder()
	server.ServeHTTP(response, signedRequest(string(body), testSigningSecret))
	return response
}

func expectInput(t *testing.T, tc *mocks.Client, wfID string, result error) *router.Input {
	t.Helper()
	var input router.Input
	tc.On("ExecuteWorkflow", mock.Anything, client.StartWorkflowOptions{
		ID: wfID, TaskQueue: "linear-task-queue",
	}, router.WorkflowName, mock.Anything).Run(func(args mock.Arguments) {
		input = args.Get(3).(router.Input)
		_, hasDeadline := args.Get(0).(context.Context).Deadline()
		assert.False(t, hasDeadline)
	}).Return(nil, result).Once()
	return &input
}

func TestCreatedEventStartsSessionWithPromptContext(t *testing.T) {
	tc := mocks.NewClient(t)
	input := expectInput(t, tc, "connector-default-linear:org-1:session-1-created", nil)
	server := NewServer(tc, "linear-task-queue", testSigningSecret, Options{})
	event := createdEvent()
	event["promptContext"] = "<issue>Bug details</issue>\n<guidance>Run the tests</guidance>"

	response := postEvent(t, server, event)

	require.Equal(t, http.StatusOK, response.Code)
	assert.Equal(t, "linear:org-1:session-1", input.Request.SessionID)
	require.NotNil(t, input.Request.Message)
	assert.Equal(t, event["promptContext"], input.Request.Message.Text)
	assert.Equal(t, "user-1", input.Request.Message.Sender)
	assert.False(t, input.Request.Message.FirstContact)
	var address linear.Address
	require.NoError(t, json.Unmarshal(input.Address, &address))
	assert.Equal(t, linear.Address{
		SessionID: "linear:org-1:session-1", AgentSessionID: "session-1", OrganizationID: "org-1",
	}, address)
}

func TestCreatedEventRequiresPromptContext(t *testing.T) {
	for _, kind := range []string{"missing", "null", "empty", "whitespace"} {
		t.Run(kind, func(t *testing.T) {
			tc := mocks.NewClient(t)
			event := createdEvent()
			switch kind {
			case "missing":
				delete(event, "promptContext")
			case "null":
				event["promptContext"] = nil
			case "empty":
				event["promptContext"] = ""
			case "whitespace":
				event["promptContext"] = " \t\n"
			}

			response := postEvent(t, NewServer(tc, "linear-task-queue", testSigningSecret, Options{}), event)

			assert.Equal(t, http.StatusBadRequest, response.Code)
			tc.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
		})
	}
}

func TestPromptedEventContinuesSessionWithNewMessage(t *testing.T) {
	tc := mocks.NewClient(t)
	input := expectInput(t, tc, "connector-default-linear:org-1:session-1-prompt-activity-1", nil)
	response := postEvent(t, NewServer(tc, "linear-task-queue", testSigningSecret, Options{}), promptedEvent())

	require.Equal(t, http.StatusOK, response.Code)
	assert.Equal(t, "linear:org-1:session-1", input.Request.SessionID)
	require.NotNil(t, input.Request.Message)
	assert.Equal(t, "What about the tests?", input.Request.Message.Text)
	assert.Equal(t, "user-2", input.Request.Message.Sender)
}

func TestSessionAndWorkflowIDsAreIsolated(t *testing.T) {
	for _, organization := range []string{"org-1", "org-2"} {
		for _, sessionID := range []string{"session-1", "session-2"} {
			t.Run(organization+"/"+sessionID, func(t *testing.T) {
				tc := mocks.NewClient(t)
				wantSession := "linear:" + organization + ":" + sessionID
				input := expectInput(t, tc, "connector-custom-"+wantSession+"-created", nil)
				event := createdEvent()
				event["organizationId"] = organization
				session := event["agentSession"].(map[string]any)
				session["id"], session["organizationId"] = sessionID, organization

				response := postEvent(t, NewServer(tc, "linear-task-queue", testSigningSecret, Options{Identity: "custom"}), event)

				require.Equal(t, http.StatusOK, response.Code)
				assert.Equal(t, wantSession, input.Request.SessionID)
			})
		}
	}
}

func TestRepeatedDeliveriesUseSameWorkflowID(t *testing.T) {
	for _, event := range []map[string]any{createdEvent(), promptedEvent()} {
		t.Run(event["action"].(string), func(t *testing.T) {
			tc := mocks.NewClient(t)
			interaction := "created"
			if event["action"] == "prompted" {
				interaction = "prompt-activity-1"
			}
			wfID := "connector-default-linear:org-1:session-1-" + interaction
			expectInput(t, tc, wfID, nil)
			expectInput(t, tc, wfID, nil)
			server := NewServer(tc, "linear-task-queue", testSigningSecret, Options{})

			assert.Equal(t, http.StatusOK, postEvent(t, server, event).Code)
			event["webhookTimestamp"] = time.Now().Add(time.Second).UnixMilli()
			assert.Equal(t, http.StatusOK, postEvent(t, server, event).Code)
		})
	}
}

func TestWorkflowStartFailureIsAcknowledged(t *testing.T) {
	var logs bytes.Buffer
	originalOutput := log.Writer()
	log.SetOutput(&logs)
	t.Cleanup(func() { log.SetOutput(originalOutput) })

	tc := mocks.NewClient(t)
	expectInput(t, tc, "connector-default-linear:org-1:session-1-created", errors.New("unavailable"))
	response := postEvent(t, NewServer(tc, "linear-task-queue", testSigningSecret, Options{}), createdEvent())
	assert.Equal(t, http.StatusOK, response.Code)
	assert.Contains(t, logs.String(), "Failed to start connector workflow connector-default-linear:org-1:session-1-created: unavailable")
}

func TestIgnoredEventsDoNotStartWorkflows(t *testing.T) {
	for _, kind := range []string{"other type", "other action", "stop", "agent response"} {
		t.Run(kind, func(t *testing.T) {
			event := promptedEvent()
			activity := event["agentActivity"].(map[string]any)
			switch kind {
			case "other type":
				event["type"] = "Comment"
			case "other action":
				event["action"] = "updated"
			case "stop":
				activity["signal"] = "stop"
			case "agent response":
				activity["content"].(map[string]any)["type"] = "response"
			}
			tc := mocks.NewClient(t)
			response := postEvent(t, NewServer(tc, "linear-task-queue", testSigningSecret, Options{}), event)
			assert.Equal(t, http.StatusOK, response.Code)
			tc.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
		})
	}
}

func TestInvalidEventsAreRejected(t *testing.T) {
	for _, kind := range []string{
		"organization", "session", "organization mismatch",
		"activity", "activity id", "activity session", "prompt", "timestamp missing", "timestamp old", "timestamp future",
	} {
		t.Run(kind, func(t *testing.T) {
			event := promptedEvent()
			session := event["agentSession"].(map[string]any)
			activity := event["agentActivity"].(map[string]any)
			status := http.StatusBadRequest
			switch kind {
			case "organization":
				event["organizationId"] = ""
			case "session":
				session["id"] = ""
			case "organization mismatch":
				session["organizationId"] = "other-org"
			case "activity":
				delete(event, "agentActivity")
			case "activity id":
				activity["id"] = ""
			case "activity session":
				activity["agentSessionId"] = "other-session"
			case "prompt":
				activity["content"].(map[string]any)["body"] = " "
			case "timestamp missing":
				delete(event, "webhookTimestamp")
				status = http.StatusUnauthorized
			case "timestamp old", "timestamp future":
				offset := -2 * time.Minute
				if kind == "timestamp future" {
					offset = -offset
				}
				event["webhookTimestamp"] = time.Now().Add(offset).UnixMilli()
				status = http.StatusUnauthorized
			}
			tc := mocks.NewClient(t)
			response := postEvent(t, NewServer(tc, "linear-task-queue", testSigningSecret, Options{}), event)
			assert.Equal(t, status, response.Code)
			tc.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
		})
	}
}

func TestInvalidSignaturesAreRejected(t *testing.T) {
	for _, kind := range []string{"missing", "malformed", "wrong secret", "tampered body", "empty secret"} {
		t.Run(kind, func(t *testing.T) {
			body, err := json.Marshal(createdEvent())
			require.NoError(t, err)
			r := signedRequest(string(body), testSigningSecret)
			secret := testSigningSecret
			switch kind {
			case "missing":
				r.Header.Del("Linear-Signature")
			case "malformed":
				r.Header.Set("Linear-Signature", "invalid")
			case "wrong secret":
				r = signedRequest(string(body), "wrong-secret")
			case "tampered body":
				r.Body = io.NopCloser(strings.NewReader(string(body) + " "))
			case "empty secret":
				secret = ""
				r = signedRequest(string(body), "")
			}
			tc := mocks.NewClient(t)
			response := httptest.NewRecorder()
			NewServer(tc, "linear-task-queue", secret, Options{}).ServeHTTP(response, r)
			assert.Equal(t, http.StatusUnauthorized, response.Code)
			tc.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
		})
	}
}

func TestRoutes(t *testing.T) {
	for _, tc := range []struct {
		name   string
		method string
		path   string
		body   string
		status int
	}{
		{"method", http.MethodGet, routeEvents, "", http.StatusMethodNotAllowed},
		{"path", http.MethodPost, "/other", "", http.StatusNotFound},
		{"json", http.MethodPost, routeEvents, "{", http.StatusBadRequest},
		{"type", http.MethodPost, routeEvents, "{}", http.StatusBadRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client := mocks.NewClient(t)
			r := signedRequest(tc.body, testSigningSecret)
			r.Method, r.URL.Path = tc.method, tc.path
			response := httptest.NewRecorder()
			NewServer(client, "linear-task-queue", testSigningSecret, Options{}).ServeHTTP(response, r)
			assert.Equal(t, tc.status, response.Code)
			client.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
		})
	}
}
