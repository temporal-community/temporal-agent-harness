package teamsoutbound

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
)

func TestFlattenDeltaText(t *testing.T) {
	cases := []struct {
		name  string
		delta router.Delta
		want  string
	}{
		{"reply text", router.Delta{Text: "hello"}, "hello"},
		{"tool started", router.Delta{ToolStatus: &router.ToolStatus{ToolName: "search", Status: router.ToolStarted}}, "\n_search..._"},
		{"tool completed", router.Delta{ToolStatus: &router.ToolStatus{ToolName: "search", Status: router.ToolCompleted}}, " ✅\n\n"},
		{"tool errored", router.Delta{ToolStatus: &router.ToolStatus{ToolName: "search", Status: router.ToolErrored, Message: "oops"}}, " ❌ Error: oops\n\n"},
		{"thought summary", router.Delta{ThoughtSummary: "thinking..."}, "thinking..."},
		{"empty delta", router.Delta{}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			assert.Equal(t, tc.want, flattenDeltaText(tc.delta))
		})
	}
}

// TestFlattenSegments_MatchesPreRefactorFlattening pins the exact string the connector
// produced back when agent/driver.go baked tool status directly into Delta.Text - Teams'
// visible output must stay identical now that this is reconstructed from structured
// ToolStatus/ThoughtSummary fields instead.
func TestFlattenSegments_MatchesPreRefactorFlattening(t *testing.T) {
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

func TestPostMessageSplicesCitationsIntoFlattenedText(t *testing.T) {
	driver := NewDriver(workflow.ActivityOptions{StartToCloseTimeout: time.Minute})
	var posted router.TextMetadata
	suite := testsuite.WorkflowTestSuite{}
	env := suite.NewTestWorkflowEnvironment()
	env.RegisterActivityWithOptions(
		func(_ context.Context, input router.TextMetadata) error {
			posted = input
			return nil
		},
		activity.RegisterOptions{Name: postMessageActivity},
	)
	workflowFn := func(ctx workflow.Context) error {
		return driver.PostMessage(ctx, router.TextMetadata{
			SessionID: "teams:conversation-1",
			Segments: []router.Delta{
				{ToolStatus: &router.ToolStatus{ToolName: "search", Status: router.ToolStarted}},
				{ToolStatus: &router.ToolStatus{ToolName: "search", Status: router.ToolCompleted}},
				{Text: "hello world"},
			},
			Citations: []router.Citation{{URL: "https://example.com/doc", Title: "Doc", EndIndex: 5}},
		})
	}
	env.RegisterWorkflow(workflowFn)

	env.ExecuteWorkflow(workflowFn)

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, "\n_search..._ ✅\n\nhello [[1]](https://example.com/doc) world", posted.Text)
}

func TestDriverSupportsStreaming(t *testing.T) {
	driver := NewDriver(workflow.ActivityOptions{})

	tests := []struct {
		name             string
		conversationType string
		expected         bool
	}{
		{name: "personal", conversationType: "personal", expected: true},
		{name: "channel", conversationType: "channel", expected: false},
		{name: "group chat", conversationType: " GROUPCHAT ", expected: false},
		{name: "unknown", conversationType: "unknown", expected: true},
		{name: "no message", expected: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			input := router.Input{}
			if tt.conversationType != "" {
				input.Message = &router.IncomingMessage{ConversationType: tt.conversationType}
			}
			assert.Equal(t, tt.expected, driver.SupportsStreaming(input))
		})
	}
}

func TestStreamActivityContextRoutesToPinnedWorker(t *testing.T) {
	driver := NewDriver(workflow.ActivityOptions{StartToCloseTimeout: time.Minute})
	type result struct {
		TaskQueue              string
		ScheduleToStartTimeout time.Duration
	}

	suite := testsuite.WorkflowTestSuite{}
	env := suite.NewTestWorkflowEnvironment()
	workflowFn := func(ctx workflow.Context) (result, error) {
		options := workflow.GetActivityOptions(driver.streamActivityContext(ctx, router.StreamHandle{
			TaskQueue: "teams-worker-1",
		}))
		return result{
			TaskQueue:              options.TaskQueue,
			ScheduleToStartTimeout: options.ScheduleToStartTimeout,
		}, nil
	}
	env.RegisterWorkflow(workflowFn)

	env.ExecuteWorkflow(workflowFn)

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	var got result
	require.NoError(t, env.GetWorkflowResult(&got))
	assert.Equal(t, "teams-worker-1", got.TaskQueue)
	assert.Equal(t, pinnedActivityScheduleToStartTimeout, got.ScheduleToStartTimeout)
}

func TestFinishStreamReturnsPinnedWorkerError(t *testing.T) {
	driver := NewDriver(workflow.ActivityOptions{
		StartToCloseTimeout: time.Minute,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
	})

	suite := testsuite.WorkflowTestSuite{}
	env := suite.NewTestWorkflowEnvironment()
	env.RegisterActivityWithOptions(
		func(context.Context, router.FinishStreamInput) error {
			return errors.New("pinned worker unavailable")
		},
		activity.RegisterOptions{Name: finishStreamActivity},
	)
	workflowFn := func(ctx workflow.Context) error {
		return driver.FinishStream(ctx, router.FinishStreamInput{
			TextMetadata: router.TextMetadata{
				SessionID:  "teams:conversation-1",
				ServiceURL: "https://example.test/teams/",
				ChannelID:  "msteams",
			},
			Handle: router.StreamHandle{
				ID:        "activity-1",
				SessionID: "teams:conversation-1",
				TaskQueue: "teams-worker-1",
			},
		})
	}
	env.RegisterWorkflow(workflowFn)

	env.ExecuteWorkflow(workflowFn)

	require.True(t, env.IsWorkflowCompleted())
	require.Error(t, env.GetWorkflowError())
	assert.ErrorContains(t, env.GetWorkflowError(), "pinned worker unavailable")
}

func TestAcknowledgeApprovalUpdatesPrompt(t *testing.T) {
	tests := []struct {
		name     string
		approved bool
		text     string
	}{
		{name: "approved", approved: true, text: "🔐 Tool `deploy`: ✅ Approved"},
		{name: "denied", approved: false, text: "🔐 Tool `deploy`: ❌ Denied"},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			driver := NewDriver(workflow.ActivityOptions{StartToCloseTimeout: time.Minute})
			var update router.UpdateMessageInput
			suite := testsuite.WorkflowTestSuite{}
			env := suite.NewTestWorkflowEnvironment()
			env.RegisterActivityWithOptions(
				func(_ context.Context, input router.UpdateMessageInput) error {
					update = input
					return nil
				},
				activity.RegisterOptions{Name: updateMessageActivity},
			)
			workflowFn := func(ctx workflow.Context) error {
				return driver.AcknowledgeApproval(ctx, router.ApprovalAcknowledgementInput{
					TextMetadata: router.TextMetadata{
						SessionID:  "teams:conversation-1",
						ServiceURL: "https://example.test/teams/",
						ChannelID:  "msteams",
					},
					PromptID: "card-1",
					ToolName: "deploy",
					Approved: test.approved,
				})
			}
			env.RegisterWorkflow(workflowFn)

			env.ExecuteWorkflow(workflowFn)

			require.True(t, env.IsWorkflowCompleted())
			require.NoError(t, env.GetWorkflowError())
			assert.Equal(t, "card-1", update.MessageID)
			assert.Equal(t, test.text, update.Text)
			assert.Equal(t, "https://example.test/teams/", update.ServiceURL)
			assert.Equal(t, "msteams", update.ChannelID)
		})
	}
}

func TestAcknowledgeApprovalWithoutPromptDoesNothing(t *testing.T) {
	driver := NewDriver(workflow.ActivityOptions{StartToCloseTimeout: time.Minute})
	suite := testsuite.WorkflowTestSuite{}
	env := suite.NewTestWorkflowEnvironment()
	workflowFn := func(ctx workflow.Context) error {
		return driver.AcknowledgeApproval(ctx, router.ApprovalAcknowledgementInput{
			ToolName: "deploy",
			Approved: true,
		})
	}
	env.RegisterWorkflow(workflowFn)

	env.ExecuteWorkflow(workflowFn)

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
}
