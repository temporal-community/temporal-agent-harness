package slackinbound

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/stretchr/testify/mock"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
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
			return input.Message != nil && input.Message.RequiresExistingSession == requiresExistingSession
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

// TestHandleEventsForwardsMention covers both a mention on a fresh top-level
// message and a mention on an existing thread reply - both should forward
// immediately without probing for a prior session.
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
			server := NewServer(mockClient, "task-queue", "", testBotUserID)

			postEvent(t, server, `{
				"type":"event_callback",
				"event":{"type":"message","channel":"C1","user":"U1","ts":"`+tc.ts+`","thread_ts":"`+tc.threadTS+`","text":"<@BOT123> hi"}
			}`)
			mockClient.AssertNotCalled(t, "ListWorkflow", mock.Anything, mock.Anything)
		})
	}
}

// TestHandleEventsForwardsUnmentionedReplyWhenThreadWasEverMentioned covers both the
// case where the thread root itself was the mention, and the trickier case where the
// mention happened on some earlier reply deep in the thread (not the root) - in both
// cases the router workflow that the mention started has a different workflow-ID
// suffix than the root's own ts, so the lookup must match on the shared thread
// prefix rather than reconstructing one specific ID.
func TestHandleEventsForwardsUnmentionedReplyWhenThreadWasEverMentioned(t *testing.T) {
	mockClient := mocks.NewClient(t)
	expectThreadSessionLookup(t, mockClient, "connector-default-slack:C1:100.000-", true)
	expectRouterWorkflowStart(t, mockClient, "connector-default-slack:C1:100.000-300.000", true)
	server := NewServer(mockClient, "task-queue", "", testBotUserID)

	postEvent(t, server, `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","ts":"300.000","thread_ts":"100.000","text":"just a reply"}
	}`)
}

func TestHandleEventsDropsUnmentionedReplyWhenThreadWasNeverMentioned(t *testing.T) {
	mockClient := mocks.NewClient(t)
	expectThreadSessionLookup(t, mockClient, "connector-default-slack:C1:100.000-", false)
	server := NewServer(mockClient, "task-queue", "", testBotUserID)

	postEvent(t, server, `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","ts":"200.000","thread_ts":"100.000","text":"just a reply"}
	}`)

	mockClient.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
}

func TestHandleEventsDropsUnmentionedTopLevelMessage(t *testing.T) {
	mockClient := mocks.NewClient(t)
	server := NewServer(mockClient, "task-queue", "", testBotUserID)

	postEvent(t, server, `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","ts":"100.000","text":"just chatting"}
	}`)

	mockClient.AssertNotCalled(t, "ListWorkflow", mock.Anything, mock.Anything)
	mockClient.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
}
