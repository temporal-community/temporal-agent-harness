package teams

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
)

func TestStreamActivityContextRoutesToPinnedWorker(t *testing.T) {
	driver := NewDriver(workflow.ActivityOptions{StartToCloseTimeout: time.Minute})
	type result struct {
		TaskQueue              string
		ScheduleToStartTimeout time.Duration
	}

	suite := testsuite.WorkflowTestSuite{}
	env := suite.NewTestWorkflowEnvironment()
	workflowFn := func(ctx workflow.Context) (result, error) {
		options := workflow.GetActivityOptions(driver.streamActivityContext(ctx, inbound.StreamHandle{
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
		func(context.Context, inbound.FinishStreamInput) error {
			return errors.New("pinned worker unavailable")
		},
		activity.RegisterOptions{Name: finishStreamActivity},
	)
	workflowFn := func(ctx workflow.Context) error {
		return driver.FinishStream(ctx, inbound.FinishStreamInput{
			TextMetadata: inbound.TextMetadata{
				SessionID:  "teams:conversation-1",
				ServiceURL: "https://example.test/teams/",
				ChannelID:  "msteams",
			},
			Handle: inbound.StreamHandle{
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
			var update inbound.UpdateMessageInput
			suite := testsuite.WorkflowTestSuite{}
			env := suite.NewTestWorkflowEnvironment()
			env.RegisterActivityWithOptions(
				func(_ context.Context, input inbound.UpdateMessageInput) error {
					update = input
					return nil
				},
				activity.RegisterOptions{Name: updateMessageActivity},
			)
			workflowFn := func(ctx workflow.Context) error {
				return driver.AcknowledgeApproval(ctx, inbound.ApprovalAcknowledgementInput{
					TextMetadata: inbound.TextMetadata{
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
		return driver.AcknowledgeApproval(ctx, inbound.ApprovalAcknowledgementInput{
			ToolName: "deploy",
			Approved: true,
		})
	}
	env.RegisterWorkflow(workflowFn)

	env.ExecuteWorkflow(workflowFn)

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
}
