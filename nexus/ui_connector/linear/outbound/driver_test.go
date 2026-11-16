package linearoutbound

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

func TestDriverRegistersAndRunsDeliveryActivities(t *testing.T) {
	f, platform := newFakeLinear(t)
	env := (&testsuite.WorkflowTestSuite{}).NewTestWorkflowEnvironment()
	RegisterActivities(env, platform)
	driver := NewDriver(workflow.ActivityOptions{StartToCloseTimeout: time.Second})
	wf := func(ctx workflow.Context) error {
		state, err := driver.Deliver(ctx, address, nil, []protocol.Event{{TurnStarted: true}, text("Hello ")})
		if err != nil {
			return err
		}
		state, err = driver.Deliver(ctx, address, state, []protocol.Event{text("world")})
		if err != nil {
			return err
		}
		return driver.Done(ctx, address, state)
	}
	env.ExecuteWorkflow(wf)
	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	require.Len(t, f.requests, 2)
	assert.Equal(t, "thought", f.requests[0].Content.Type)
	assert.Equal(t, activityContent{Type: "response", Body: "Hello world"}, f.requests[1].Content)
}

func TestDriverDoneWithoutStateSkipsActivity(t *testing.T) {
	env := (&testsuite.WorkflowTestSuite{}).NewTestWorkflowEnvironment()
	driver := NewDriver(workflow.ActivityOptions{StartToCloseTimeout: time.Second})
	wf := func(ctx workflow.Context) error {
		for _, state := range []json.RawMessage{nil, json.RawMessage("null")} {
			if err := driver.Done(ctx, address, state); err != nil {
				return err
			}
		}
		return nil
	}
	// No activities are registered: executing one would fail the test.
	env.ExecuteWorkflow(wf)
	require.NoError(t, env.GetWorkflowError())
}

func TestDriverPropagatesDeliveryAndDoneFailures(t *testing.T) {
	for _, end := range []bool{false, true} {
		t.Run(map[bool]string{false: "Deliver", true: "Done"}[end], func(t *testing.T) {
			f, platform := newFakeLinear(t)
			f.status = 503
			env := (&testsuite.WorkflowTestSuite{}).NewTestWorkflowEnvironment()
			RegisterActivities(env, platform)
			driver := NewDriver(workflow.ActivityOptions{
				StartToCloseTimeout: time.Second,
				RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
			})
			wf := func(ctx workflow.Context) error {
				if end {
					return driver.Done(ctx, address, json.RawMessage(`{"Text":"answer"}`))
				}
				_, err := driver.Deliver(ctx, json.RawMessage("[]"), nil, []protocol.Event{text("answer")})
				return err
			}
			env.ExecuteWorkflow(wf)
			if end {
				require.ErrorContains(t, env.GetWorkflowError(), "HTTP 503")
				assert.Len(t, f.requests, 1)
			} else {
				require.ErrorContains(t, env.GetWorkflowError(), "decode address")
				assert.Empty(t, f.requests)
			}
		})
	}
}
