package router

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/wire"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
)

// fakeOutbound is a minimal outbound.Driver test double: a canned StartTurn result,
// plus one PollResult per call to PollTurn (Closed thereafter).
type fakeOutbound struct {
	startResult outbound.StartResult
	startErr    error
	pollResults []outbound.PollResult
	pollCalls   int
}

func (f *fakeOutbound) StartTurn(ctx workflow.Context, input wire.Input) (outbound.StartResult, error) {
	return f.startResult, f.startErr
}

func (f *fakeOutbound) PollTurn(ctx workflow.Context, handle outbound.TurnHandle, cursor int64) (outbound.PollResult, error) {
	if f.pollCalls >= len(f.pollResults) {
		return outbound.PollResult{Closed: true}, nil
	}
	res := f.pollResults[f.pollCalls]
	f.pollCalls++
	return res, nil
}

// fakeInbound is a minimal inbound.Driver test double that records calls in order.
type fakeInbound struct {
	calls          []string
	streamStartErr error
}

func (f *fakeInbound) Stream(ctx workflow.Context, input inbound.StreamInput) (string, error) {
	switch input.DeltaType {
	case inbound.DeltaTypeStart:
		f.calls = append(f.calls, "Start")
		if f.streamStartErr != nil {
			return "", f.streamStartErr
		}
		return "stream-1", nil
	case inbound.DeltaTypeEnd:
		f.calls = append(f.calls, "End")
		return input.StreamID, nil
	default:
		f.calls = append(f.calls, "Append:"+input.Text)
		return input.StreamID, nil
	}
}

func (f *fakeInbound) PostMessage(ctx workflow.Context, input inbound.TextMetadata) error {
	f.calls = append(f.calls, "PostMessage:"+input.Text)
	return nil
}

func (f *fakeInbound) PostApprovalPrompt(ctx workflow.Context, input inbound.ApprovalPromptInput) error {
	f.calls = append(f.calls, "PostApprovalPrompt:"+input.ToolName)
	return nil
}

func defaultInput() wire.Input {
	return wire.Input{
		Identity:  "default",
		SessionID: "slack:C12345",
		Message:   &wire.IncomingMessage{MessageID: "m1", Text: "hello"},
	}
}

func newTestEnv(t *testing.T, w *RouterWorkflow) *testsuite.TestWorkflowEnvironment {
	t.Helper()
	s := testsuite.WorkflowTestSuite{}
	env := s.NewTestWorkflowEnvironment()
	env.RegisterWorkflow(w.Run)
	return env
}

func TestRouterWorkflow_MessageTurn_StreamsDeltas(t *testing.T) {
	handle := outbound.TurnHandle{TurnNumber: 1}
	out := &fakeOutbound{
		startResult: outbound.StartResult{Handle: &handle},
		pollResults: []outbound.PollResult{
			{Deltas: []outbound.Delta{{Text: "hello "}, {Text: "world", IsFinal: true}}},
		},
	}
	in := &fakeInbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"Start", "Append:hello ", "Append:world", "End"}, in.calls)
}

func TestRouterWorkflow_SynchronousReply_PostsMessageWithoutPolling(t *testing.T) {
	out := &fakeOutbound{startResult: outbound.StartResult{Reply: "pong"}}
	in := &fakeInbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"PostMessage:pong"}, in.calls)
	assert.Equal(t, 0, out.pollCalls, "a synchronous reply must not poll")
}

func TestRouterWorkflow_FireAndForget_DoesNothingFurther(t *testing.T) {
	out := &fakeOutbound{startResult: outbound.StartResult{}}
	in := &fakeInbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, wire.Input{
		Identity:  "default",
		SessionID: "slack:C12345",
		Approval:  &wire.ApprovalDecision{ToolID: "t1", Approved: true},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Empty(t, in.calls)
	assert.Equal(t, 0, out.pollCalls)
}

func TestRouterWorkflow_ApprovalRequestedDelta_PostsPrompt(t *testing.T) {
	handle := outbound.TurnHandle{}
	out := &fakeOutbound{
		startResult: outbound.StartResult{Handle: &handle},
		pollResults: []outbound.PollResult{
			{Deltas: []outbound.Delta{
				{ApprovalRequested: &outbound.ApprovalRequest{ToolID: "t1", ToolName: "search"}},
				{Text: "done", IsFinal: true},
			}},
		},
	}
	in := &fakeInbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"PostApprovalPrompt:search", "Start", "Append:done", "End"}, in.calls)
}

func TestRouterWorkflow_StreamStartFails_FallsBackToPostMessage(t *testing.T) {
	handle := outbound.TurnHandle{}
	out := &fakeOutbound{
		startResult: outbound.StartResult{Handle: &handle},
		pollResults: []outbound.PollResult{
			{Deltas: []outbound.Delta{{Text: "partial"}}},
		},
	}
	in := &fakeInbound{streamStartErr: assert.AnError}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"Start", "PostMessage:partial"}, in.calls)
}
