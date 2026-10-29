package slackinbound

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	slackapi "github.com/slack-go/slack"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/mock"
	"github.com/stretchr/testify/require"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/slack/interaction"
	workflowpb "go.temporal.io/api/workflow/v1"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/mocks"
)

const testBotUserID = "BOT123"

func postEvent(t *testing.T, s *webhookServer, body string) {
	t.Helper()
	request := httptest.NewRequest(http.MethodPost, routeEvents, strings.NewReader(body))
	response := httptest.NewRecorder()
	s.handleEvents(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", response.Code)
	}
}

func expectRouterWorkflowStart(t *testing.T, tc *mocks.Client, wfID string, requiresExistingSession bool) {
	t.Helper()
	tc.On(
		"ExecuteWorkflow",
		mock.Anything,
		mock.MatchedBy(func(options client.StartWorkflowOptions) bool {
			return options.ID == wfID
		}),
		router.WorkflowName,
		mock.MatchedBy(func(input router.Input) bool {
			m := input.Request.Message
			return m != nil && m.RequiresExistingSession == requiresExistingSession
		}),
	).Return(nil, nil).Once()
}

// expectThreadSessionLookup mocks the ListWorkflow prefix search threadHasBotSession
// issues for a given thread, returning `found` executions.
func expectThreadSessionLookup(t *testing.T, tc *mocks.Client, wfIDPrefix string, found bool) {
	t.Helper()
	var executions []*workflowpb.WorkflowExecutionInfo
	if found {
		executions = []*workflowpb.WorkflowExecutionInfo{{}}
	}
	tc.On(
		"ListWorkflow",
		mock.Anything,
		mock.MatchedBy(func(req *workflowservice.ListWorkflowExecutionsRequest) bool {
			return req.Query == `WorkflowId STARTS_WITH "`+wfIDPrefix+`"`
		}),
	).Return(&workflowservice.ListWorkflowExecutionsResponse{Executions: executions}, nil).Once()
}

func TestHandleEventsForwardsMention(t *testing.T) {
	cases := []struct {
		name     string
		ts       string
		threadTS string
		wfID     string
	}{
		{"top-level", "100.000", "", "connector-default-slack:C1:100.000-100.000"},
		{"thread-reply", "200.000", "100.000", "connector-default-slack:C1:100.000-200.000"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			mockClient := mocks.NewClient(t)
			expectRouterWorkflowStart(t, mockClient, tc.wfID, false)
			server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID})

			postEvent(t, server, `{
				"type":"event_callback",
				"event":{"type":"message","channel":"C1","user":"U1","ts":"`+tc.ts+`","thread_ts":"`+tc.threadTS+`","text":"<@BOT123> hi"}
			}`)
			mockClient.AssertNotCalled(t, "ListWorkflow", mock.Anything, mock.Anything)
		})
	}
}

// Find prior bot interactions by the thread prefix, regardless of which reply mentioned the bot.
func TestHandleEventsForwardsUnmentionedReplyWhenThreadWasEverMentioned(t *testing.T) {
	mockClient := mocks.NewClient(t)
	expectThreadSessionLookup(t, mockClient, "connector-default-slack:C1:100.000-", true)
	expectRouterWorkflowStart(t, mockClient, "connector-default-slack:C1:100.000-300.000", true)
	server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID})

	postEvent(t, server, `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","ts":"300.000","thread_ts":"100.000","text":"just a reply"}
	}`)
}

func TestHandleEventsDropsUnmentionedReplyWhenThreadWasNeverMentioned(t *testing.T) {
	mockClient := mocks.NewClient(t)
	expectThreadSessionLookup(t, mockClient, "connector-default-slack:C1:100.000-", false)
	server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID})

	postEvent(t, server, `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","ts":"200.000","thread_ts":"100.000","text":"just a reply"}
	}`)

	mockClient.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
}

func TestHandleEventsDropsUnmentionedTopLevelMessage(t *testing.T) {
	mockClient := mocks.NewClient(t)
	server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID})

	postEvent(t, server, `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","ts":"100.000","text":"just chatting"}
	}`)

	mockClient.AssertNotCalled(t, "ListWorkflow", mock.Anything, mock.Anything)
	mockClient.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
}

func TestHandleEventsForwardsAllowedBotMention(t *testing.T) {
	for _, botID := range []string{"HCBOT1", "HCBOT2"} {
		t.Run(botID, func(t *testing.T) {
			mockClient := mocks.NewClient(t)
			expectRouterWorkflowStart(t, mockClient, "connector-default-slack:C1:100.000-100.000", false)
			server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID, AllowedBotIDs: []string{"HCBOT1", "HCBOT2"}})

			postEvent(t, server, `{
				"type":"event_callback",
				"event":{"type":"message","channel":"C1","user":"U1","bot_id":"`+botID+`","ts":"100.000","text":"<@BOT123> hi"}
			}`)
		})
	}
}

func TestHandleEventsDropsOtherBotMentionEvenWhenSomeBotsAreAllowed(t *testing.T) {
	mockClient := mocks.NewClient(t)
	server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID, AllowedBotIDs: []string{"HCBOT1", "HCBOT2"}})

	postEvent(t, server, `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","bot_id":"OTHERBOT","ts":"100.000","text":"<@BOT123> hi"}
	}`)

	mockClient.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
}

// expectInput mocks one workflow start and captures its input.
func expectInput(tc *mocks.Client, wfID string) *router.Input {
	var got router.Input
	tc.On("ExecuteWorkflow", mock.Anything,
		mock.MatchedBy(func(options client.StartWorkflowOptions) bool { return options.ID == wfID }),
		router.WorkflowName, mock.Anything,
	).Run(func(args mock.Arguments) { got = args.Get(3).(router.Input) }).Return(nil, nil).Once()
	return &got
}

func TestHandleEventsStripsTheBotMention(t *testing.T) {
	mockClient := mocks.NewClient(t)
	got := expectInput(mockClient, "connector-default-slack:C1:100.000-100.000")
	server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID})

	postEvent(t, server, `{"type":"event_callback","event":{"type":"message","channel":"C1","user":"U1","ts":"100.000","text":"<@BOT123> /help"}}`)
	require.NotNil(t, got.Request.Message)
	assert.Equal(t, "/help", got.Request.Message.Text)
	assert.JSONEq(t, `{"sessionId":"slack:C1:100.000","channel":"C1","threadTs":"100.000","userId":"U1"}`, string(got.Address))
}

func TestHandleEventsMarksFirstContact(t *testing.T) {
	t.Run("top-level mention starts a session", func(t *testing.T) {
		mockClient := mocks.NewClient(t)
		got := expectInput(mockClient, "connector-default-slack:C1:100.000-100.000")
		server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID, GuideOnFirstContact: true})

		postEvent(t, server, `{"type":"event_callback","event":{"type":"message","channel":"C1","user":"U1","ts":"100.000","text":"<@BOT123> hi"}}`)
		assert.True(t, got.Request.Message.FirstContact)
		mockClient.AssertNotCalled(t, "ListWorkflow", mock.Anything, mock.Anything)
	})

	for _, known := range []bool{false, true} {
		t.Run(fmt.Sprintf("in-thread mention, session known=%v", known), func(t *testing.T) {
			mockClient := mocks.NewClient(t)
			expectThreadSessionLookup(t, mockClient, "connector-default-slack:C1:100.000-", known)
			got := expectInput(mockClient, "connector-default-slack:C1:100.000-200.000")
			server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID, GuideOnFirstContact: true})

			postEvent(t, server, `{"type":"event_callback","event":{"type":"message","channel":"C1","user":"U1","ts":"200.000","thread_ts":"100.000","text":"<@BOT123> hi"}}`)
			assert.Equal(t, !known, got.Request.Message.FirstContact)
		})
	}
}

func postInteraction(t *testing.T, s *webhookServer, payload any) *httptest.ResponseRecorder {
	t.Helper()
	body, err := json.Marshal(payload)
	require.NoError(t, err)
	form := url.Values{"payload": {string(body)}}
	request := httptest.NewRequest(http.MethodPost, routeInteractions, strings.NewReader(form.Encode()))
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	response := httptest.NewRecorder()
	s.handleInteractions(response, request)
	require.Equal(t, http.StatusOK, response.Code)
	return response
}

func TestHandleInteractionsApprovalClick(t *testing.T) {
	var replaced map[string]any
	mockClient := mocks.NewClient(t)
	got := expectInput(mockClient, "connector-default-slack:C1:1.0-approval-t1")
	server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID})
	server.responseClient.Transport = responseTransport(func(r *http.Request) (*http.Response, error) {
		assert.Equal(t, "https://hooks.slack.com/actions/T1/B1/token", r.URL.String())
		body, err := io.ReadAll(r.Body)
		require.NoError(t, err)
		require.NoError(t, json.Unmarshal(body, &replaced))
		return &http.Response{StatusCode: http.StatusOK, Body: http.NoBody}, nil
	})

	postInteraction(t, server, map[string]any{
		"type":         "block_actions",
		"user":         map[string]any{"id": "U9"},
		"response_url": "https://hooks.slack.com/actions/T1/B1/token",
		"actions": []any{map[string]any{
			"block_id":  "tool_approval",
			"action_id": interaction.ActionAlwaysAllow,
			"value":     `{"s":"slack:C1:1.0","t":"t1","n":"search","a":true,"r":true}`,
		}},
	})

	require.NotNil(t, got.Request.Approval)
	assert.Equal(t, protocol.ApprovalDecision{
		ToolID: "t1", ToolName: "search", Approved: true, Remember: true,
		Reason: "Always allowed by <@U9> in Slack.",
	}, *got.Request.Approval)
	assert.Equal(t, true, replaced["replace_original"])
	assert.Contains(t, replaced["text"], "Always allowed by <@U9>")
}

func TestHandleInteractionsFormClick(t *testing.T) {
	mockClient := mocks.NewClient(t)
	got := expectInput(mockClient, "connector-default-slack:C1:1.0-form-trig")
	server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID})

	postInteraction(t, server, map[string]any{
		"type":       "block_actions",
		"user":       map[string]any{"id": "U9"},
		"trigger_id": "trig",
		"actions": []any{map[string]any{
			"block_id":  "guide",
			"action_id": interaction.ActionOpenFormPrefix + "set_model",
			"value":     `{"s":"slack:C1:1.0","h":"set_model"}`,
		}},
	})

	require.NotNil(t, got.Request.OpenForm)
	assert.Equal(t, protocol.FormRequest{Handler: "set_model"}, *got.Request.OpenForm)
	assert.JSONEq(t, `{"sessionId":"slack:C1:1.0","channel":"C1","threadTs":"1.0","userId":"U9","triggerId":"trig"}`, string(got.Address))
}

func TestHandleInteractionsFormClickWithFieldsOpensTheModalDirectly(t *testing.T) {
	var opened string
	slackAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		require.Equal(t, "/views.open", r.URL.Path)
		body, _ := io.ReadAll(r.Body)
		opened = string(body)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer slackAPI.Close()

	mockClient := mocks.NewClient(t)
	server := NewServer(mockClient, "task-queue", "", Options{
		BotUserID: testBotUserID,
		Slack:     slackapi.New("x", slackapi.OptionAPIURL(slackAPI.URL+"/")),
	})

	postInteraction(t, server, map[string]any{
		"type":       "block_actions",
		"user":       map[string]any{"id": "U9"},
		"trigger_id": "trig",
		"actions": []any{map[string]any{
			"block_id":  "guide",
			"action_id": interaction.ActionOpenFormPrefix + "set_model",
			"value":     `{"s":"slack:C1:1.0","h":"set_model","i":true,"f":[{"n":"model","k":"enum","r":true,"o":["a","b"]}]}`,
		}},
	})

	mockClient.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
	assert.Contains(t, opened, `"trigger_id":"trig"`)
	assert.Contains(t, opened, `\"h\":\"set_model\"`, "the modal carries its metadata")
}

func formSubmission(values map[string]any) map[string]any {
	return map[string]any{
		"type": "view_submission",
		"user": map[string]any{"id": "U9"},
		"view": map[string]any{
			"id":               "V1",
			"callback_id":      interaction.FormCallbackID,
			"private_metadata": `{"s":"slack:C1:1.0","th":"1.0","h":"configure"}`,
			"state":            map[string]any{"values": values},
		},
	}
}

func TestHandleInteractionsFormSubmission(t *testing.T) {
	mockClient := mocks.NewClient(t)
	got := expectInput(mockClient, "connector-default-slack:C1:1.0-call-V1")
	server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID})

	response := postInteraction(t, server, formSubmission(map[string]any{
		"count": map[string]any{"field:integer": map[string]any{"value": "3"}},
	}))

	assert.Empty(t, response.Body.String(), "an empty 200 closes the modal")
	require.NotNil(t, got.Request.Call)
	assert.Equal(t, protocol.HandlerCall{Name: "configure", Payload: `{"count":3}`}, *got.Request.Call)
	assert.JSONEq(t, `{"sessionId":"slack:C1:1.0","channel":"C1","threadTs":"1.0","userId":"U9"}`, string(got.Address))
}

func TestHandleInteractionsFormSubmissionErrorsKeepTheModalOpen(t *testing.T) {
	mockClient := mocks.NewClient(t)
	server := NewServer(mockClient, "task-queue", "", Options{BotUserID: testBotUserID})

	response := postInteraction(t, server, formSubmission(map[string]any{
		"tags": map[string]any{"field:json": map[string]any{"value": "[1,"}},
	}))

	var body map[string]any
	require.NoError(t, json.Unmarshal(response.Body.Bytes(), &body))
	assert.Equal(t, "errors", body["response_action"])
	assert.Contains(t, body["errors"].(map[string]any)["tags"], "Invalid JSON")
	mockClient.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
}

func TestMentionIsNormalizedBeforeBackendDispatch(t *testing.T) {
	tc := mocks.NewClient(t)
	tc.On("ExecuteWorkflow", mock.Anything, mock.Anything, router.WorkflowName,
		mock.MatchedBy(func(input router.Input) bool {
			return input.Request.Message != nil && input.Request.Message.Text == "/set_model model=fast"
		})).Return(nil, nil).Once()
	server := NewServer(tc, "queue", "", Options{BotUserID: testBotUserID})
	postEvent(t, server, `{"type":"event_callback","event":{"type":"message","channel":"C1","user":"U1","ts":"100.000","text":"<@BOT123> /set_model model=fast"}}`)
}
