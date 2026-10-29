package teamsinbound

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/mock"
	"github.com/stretchr/testify/require"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/teams"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/mocks"
)

func expectWorkflowStart(
	t *testing.T,
	tc *mocks.Client,
	wfID string,
	matchesInput func(router.Input) bool,
) {
	t.Helper()
	tc.On(
		"ExecuteWorkflow",
		mock.Anything,
		mock.MatchedBy(func(options client.StartWorkflowOptions) bool {
			return options.ID == wfID && options.TaskQueue == "teams-task-queue"
		}),
		router.WorkflowName,
		mock.MatchedBy(matchesInput),
	).Return(nil, nil).Once()
}

func TestHandleMessagesStartsMessageWorkflow(t *testing.T) {
	tc := mocks.NewClient(t)
	expectWorkflowStart(t, tc, "connector-default-teams:conversation-1-message-1", func(input router.Input) bool {
		return input.Request.Message != nil && input.Request.Approval == nil && input.Request.Message.Text == "question"
	})
	server := NewServer(tc, "teams-task-queue")
	request := httptest.NewRequest(http.MethodPost, routeMessages, strings.NewReader(`{
		"type":"message",
		"id":"message-1",
		"text":"question",
		"from":{"id":"user-1"},
		"conversation":{"id":"conversation-1","conversationType":"personal"}
	}`))
	response := httptest.NewRecorder()

	server.ServeHTTP(response, request)

	assert.Equal(t, http.StatusOK, response.Code)
}

func TestHandleMessagesStartsApprovalWorkflowWithEmptyText(t *testing.T) {
	tc := mocks.NewClient(t)
	expectWorkflowStart(t, tc, "connector-default-teams:conversation-1-approval-tool-1", func(input router.Input) bool {
		return input.Request.Message == nil && input.Request.Approval != nil && input.Request.Approval.ToolID == "tool-1"
	})
	server := NewServer(tc, "teams-task-queue")
	request := httptest.NewRequest(http.MethodPost, routeMessages, strings.NewReader(`{
		"type":"message",
		"replyToId":"card-1",
		"value":{"s":"teams:conversation-1","t":"tool-1","n":"deploy","a":true}
	}`))
	response := httptest.NewRecorder()

	server.ServeHTTP(response, request)

	assert.Equal(t, http.StatusOK, response.Code)
}

func TestHandleMessagesRejectsEmptyOrdinaryMessage(t *testing.T) {
	tc := mocks.NewClient(t)
	server := NewServer(tc, "teams-task-queue")
	request := httptest.NewRequest(http.MethodPost, routeMessages, strings.NewReader(`{
		"type":"message",
		"id":"message-1",
		"from":{"id":"user-1"},
		"conversation":{"id":"conversation-1"}
	}`))
	response := httptest.NewRecorder()

	server.ServeHTTP(response, request)

	assert.Equal(t, http.StatusBadRequest, response.Code)
	tc.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
}

func TestHandleMessagesRejectsMessageWithoutConversation(t *testing.T) {
	tc := mocks.NewClient(t)
	server := NewServer(tc, "teams-task-queue")
	request := httptest.NewRequest(http.MethodPost, routeMessages, strings.NewReader(`{
		"type":"message",
		"id":"message-1",
		"text":"question",
		"from":{"id":"user-1"}
	}`))
	response := httptest.NewRecorder()

	server.ServeHTTP(response, request)

	assert.Equal(t, http.StatusBadRequest, response.Code)
	tc.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
}

func TestMessageWorkflowInputPropagatesConversationType(t *testing.T) {
	for _, conversationType := range []string{"personal", "channel", "groupChat"} {
		t.Run(conversationType, func(t *testing.T) {
			var activity teamMessageActivity
			require.NoError(t, json.Unmarshal([]byte(`{
				"type":"message",
				"id":"message-1",
				"text":"question",
				"serviceUrl":"https://example.test/teams/",
				"channelId":"msteams",
				"from":{"id":"user-1"},
				"conversation":{"id":"conversation-1","conversationType":"`+conversationType+`"}
			}`), &activity))

			workflowID, input := messageWorkflowInput(activity)

			assert.Equal(t, "connector-default-teams:conversation-1-message-1", workflowID)
			require.NotNil(t, input.Request.Message)
			addr := address(t, input)
			assert.Equal(t, conversationType, addr.ConversationType)
			assert.Equal(t, "https://example.test/teams/", addr.ServiceURL)
			assert.Equal(t, "msteams", addr.ChannelID)
		})
	}
}

func TestApprovalWorkflowInputPropagatesCardRouting(t *testing.T) {
	activity := teamMessageActivity{
		ReplyToID:  "card-1",
		ServiceURL: "https://example.test/teams/",
		ChannelID:  "msteams",
	}
	value := approvalButtonValue{
		SessionID: "teams:conversation-1",
		ToolID:    "tool-1",
		ToolName:  "deploy",
		Approved:  true,
	}

	workflowID, input := approvalWorkflowInput(activity, value)

	assert.Equal(t, "connector-default-teams:conversation-1-approval-tool-1", workflowID)
	require.NotNil(t, input.Request.Approval)
	addr := address(t, input)
	assert.Equal(t, "card-1", addr.ActivityID)
	assert.Equal(t, "https://example.test/teams/", addr.ServiceURL)
	assert.Equal(t, "msteams", addr.ChannelID)
}

func address(t *testing.T, input router.Input) teams.Address {
	t.Helper()
	var addr teams.Address
	require.NoError(t, json.Unmarshal(input.Address, &addr))
	return addr
}
