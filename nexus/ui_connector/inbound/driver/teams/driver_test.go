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

func TestFinishStreamFallsBackToSharedQueueUpdate(t *testing.T) {
	driver := NewDriver(workflow.ActivityOptions{StartToCloseTimeout: time.Minute})
	var recovery inbound.UpdateMessageInput

	suite := testsuite.WorkflowTestSuite{}
	env := suite.NewTestWorkflowEnvironment()
	env.RegisterActivityWithOptions(
		func(context.Context, inbound.FinishStreamInput) error {
			return errors.New("pinned worker unavailable")
		},
		activity.RegisterOptions{Name: finishStreamActivity},
	)
	env.RegisterActivityWithOptions(
		func(_ context.Context, input inbound.UpdateMessageInput) error {
			recovery = input
			return nil
		},
		activity.RegisterOptions{Name: updateMessageActivity},
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
			FullText: "complete answer",
		})
	}
	env.RegisterWorkflow(workflowFn)

	env.ExecuteWorkflow(workflowFn)

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, "activity-1", recovery.MessageID)
	assert.Equal(t, "complete answer", recovery.Text)
	assert.Equal(t, "https://example.test/teams/", recovery.ServiceURL)
}
