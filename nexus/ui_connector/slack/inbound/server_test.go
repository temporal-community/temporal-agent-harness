package slackinbound

import (
	"net/http"
	"net/http/httptest"
	"net/url"
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

// postSlashCommand sends a slash-command form body to handleSlashCommands.
// It skips signature verification, like postEvent does.
func postSlashCommand(t *testing.T, s *webhookServer, form url.Values) *httptest.ResponseRecorder {
	t.Helper()
	request := httptest.NewRequest(http.MethodPost, routeCommands, strings.NewReader(form.Encode()))
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	response := httptest.NewRecorder()
	s.handleSlashCommands(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", response.Code)
	}
	return response
}

// expectSlashWorkflowStart mocks the workflow start for a slash command.
// It checks the final command name and arg, after prefix stripping.
func expectSlashWorkflowStart(t *testing.T, tc *mocks.Client, name, arg string) {
	t.Helper()
	tc.On(
		"ExecuteWorkflow",
		mock.Anything,
		mock.Anything,
		router.WorkflowName,
		mock.MatchedBy(func(input router.Input) bool {
			return input.Slash != nil && input.Slash.Name == name && input.Slash.Arg == arg
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
			server := NewServer(mockClient, "task-queue", "", "", testBotUserID, "", nil)

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
	server := NewServer(mockClient, "task-queue", "", "", testBotUserID, "", nil)

	postEvent(t, server, `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","ts":"300.000","thread_ts":"100.000","text":"just a reply"}
	}`)
}

func TestHandleEventsDropsUnmentionedReplyWhenThreadWasNeverMentioned(t *testing.T) {
	mockClient := mocks.NewClient(t)
	expectThreadSessionLookup(t, mockClient, "connector-default-slack:C1:100.000-", false)
	server := NewServer(mockClient, "task-queue", "", "", testBotUserID, "", nil)

	postEvent(t, server, `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","ts":"200.000","thread_ts":"100.000","text":"just a reply"}
	}`)

	mockClient.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
}

func TestHandleEventsDropsUnmentionedTopLevelMessage(t *testing.T) {
	mockClient := mocks.NewClient(t)
	server := NewServer(mockClient, "task-queue", "", "", testBotUserID, "", nil)

	postEvent(t, server, `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","ts":"100.000","text":"just chatting"}
	}`)

	mockClient.AssertNotCalled(t, "ListWorkflow", mock.Anything, mock.Anything)
	mockClient.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
}

// TestHandleEventsForwardsAllowedBotMention checks that a mention from any
// bot in allowedBotIDs is forwarded, same as a human mention - covers both
// entries in a multi-bot allowlist, not just a single configured ID.
func TestHandleEventsForwardsAllowedBotMention(t *testing.T) {
	for _, botID := range []string{"HCBOT1", "HCBOT2"} {
		t.Run(botID, func(t *testing.T) {
			mockClient := mocks.NewClient(t)
			expectRouterWorkflowStart(t, mockClient, "connector-default-slack:C1:100.000-100.000", false)
			server := NewServer(mockClient, "task-queue", "", "", testBotUserID, "", []string{"HCBOT1", "HCBOT2"})

			postEvent(t, server, `{
				"type":"event_callback",
				"event":{"type":"message","channel":"C1","user":"U1","bot_id":"`+botID+`","ts":"100.000","text":"<@BOT123> hi"}
			}`)
		})
	}
}

// TestHandleEventsDropsOtherBotMentionEvenWhenSomeBotsAreAllowed checks the
// allowlist doesn't open the door to every bot, only the configured ones.
func TestHandleEventsDropsOtherBotMentionEvenWhenSomeBotsAreAllowed(t *testing.T) {
	mockClient := mocks.NewClient(t)
	server := NewServer(mockClient, "task-queue", "", "", testBotUserID, "", []string{"HCBOT1", "HCBOT2"})

	postEvent(t, server, `{
		"type":"event_callback",
		"event":{"type":"message","channel":"C1","user":"U1","bot_id":"OTHERBOT","ts":"100.000","text":"<@BOT123> hi"}
	}`)

	mockClient.AssertNotCalled(t, "ExecuteWorkflow", mock.Anything, mock.Anything, mock.Anything, mock.Anything)
}

// TestHandleSlashCommandsStripsConfiguredPrefix checks that a prefixed
// command, like "/bot-build-name-cmd", resolves to "cmd".
func TestHandleSlashCommandsStripsConfiguredPrefix(t *testing.T) {
	mockClient := mocks.NewClient(t)
	expectSlashWorkflowStart(t, mockClient, "scope", "docs")
	server := NewServer(mockClient, "task-queue", "", "", testBotUserID, "bot-build-name", nil)

	postSlashCommand(t, server, url.Values{
		"command":    {"/bot-build-name-scope"},
		"channel_id": {"C1"},
		"trigger_id": {"T1"},
		"text":       {"docs"},
	})
}

// TestHandleSlashCommandsLeavesCommandUnchangedWhenNoPrefixConfigured checks
// that with no prefix set, the command name does not change.
func TestHandleSlashCommandsLeavesCommandUnchangedWhenNoPrefixConfigured(t *testing.T) {
	mockClient := mocks.NewClient(t)
	expectSlashWorkflowStart(t, mockClient, "scope", "docs")
	server := NewServer(mockClient, "task-queue", "", "", testBotUserID, "", nil)

	postSlashCommand(t, server, url.Values{
		"command":    {"/scope"},
		"channel_id": {"C1"},
		"trigger_id": {"T1"},
		"text":       {"docs"},
	})
}

// TestHandleSlashCommandsLeavesMismatchedPrefixUnstripped checks that a
// command missing the expected prefix does not change. It is forwarded as-is.
func TestHandleSlashCommandsLeavesMismatchedPrefixUnstripped(t *testing.T) {
	mockClient := mocks.NewClient(t)
	expectSlashWorkflowStart(t, mockClient, "scope", "docs")
	server := NewServer(mockClient, "task-queue", "", "", testBotUserID, "bot-build-id", nil)

	postSlashCommand(t, server, url.Values{
		"command":    {"/scope"},
		"channel_id": {"C1"},
		"trigger_id": {"T1"},
		"text":       {"docs"},
	})
}
