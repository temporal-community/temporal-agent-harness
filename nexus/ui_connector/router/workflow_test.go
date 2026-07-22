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
	calls           []string
	streamStartErr  error
	streamUpdateErr error
	streamHandle    *inbound.StreamHandle
	beginInputs     []inbound.BeginStreamInput
	updateInputs    []inbound.UpdateStreamInput
	finishInputs    []inbound.FinishStreamInput
	approvalInputs  []inbound.ApprovalAcknowledgementInput
}

func (f *fakeInbound) BeginStream(ctx workflow.Context, input inbound.BeginStreamInput) (inbound.StreamHandle, error) {
	f.calls = append(f.calls, "Start")
	f.beginInputs = append(f.beginInputs, input)
	if f.streamStartErr != nil {
		return inbound.StreamHandle{}, f.streamStartErr
	}
	if f.streamHandle != nil {
		handle := *f.streamHandle
		handle.SessionID = input.SessionID
		return handle, nil
	}
	return inbound.StreamHandle{
		ID:        "stream-1",
		SessionID: input.SessionID,
	}, nil
}

func (f *fakeInbound) UpdateStream(ctx workflow.Context, input inbound.UpdateStreamInput) error {
	f.calls = append(f.calls, "Append:"+input.Delta)
	f.updateInputs = append(f.updateInputs, input)
	return f.streamUpdateErr
}

func (f *fakeInbound) FinishStream(ctx workflow.Context, input inbound.FinishStreamInput) error {
	f.calls = append(f.calls, "End")
	f.finishInputs = append(f.finishInputs, input)
	return nil
}

func (f *fakeInbound) PostMessage(ctx workflow.Context, input inbound.TextMetadata) error {
	f.calls = append(f.calls, "PostMessage:"+input.Text)
	return nil
}

func (f *fakeInbound) PostApprovalPrompt(ctx workflow.Context, input inbound.ApprovalPromptInput) error {
	f.calls = append(f.calls, "PostApprovalPrompt:"+input.ToolName)
	return nil
}

func (f *fakeInbound) AcknowledgeApproval(ctx workflow.Context, input inbound.ApprovalAcknowledgementInput) error {
	f.calls = append(f.calls, "AcknowledgeApproval:"+input.ToolName)
	f.approvalInputs = append(f.approvalInputs, input)
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
		Slash:     &wire.SlashCommand{Name: "noop"},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Empty(t, in.calls)
	assert.Equal(t, 0, out.pollCalls)
}

func TestRouterWorkflow_ApprovalAcknowledgesInboundDriver(t *testing.T) {
	out := &fakeOutbound{startResult: outbound.StartResult{}}
	in := &fakeInbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, wire.Input{
		Identity:  "default",
		SessionID: "teams:conversation-1",
		Approval: &wire.ApprovalDecision{
			ToolID:     "tool-1",
			ToolName:   "deploy",
			Approved:   true,
			ActivityID: "card-1",
			ServiceURL: "https://example.test/teams/",
			ChannelID:  "msteams",
		},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"AcknowledgeApproval:deploy"}, in.calls)
	require.Len(t, in.approvalInputs, 1)
	assert.Equal(t, "card-1", in.approvalInputs[0].PromptID)
	assert.Equal(t, "deploy", in.approvalInputs[0].ToolName)
	assert.True(t, in.approvalInputs[0].Approved)
	assert.Equal(t, "https://example.test/teams/", in.approvalInputs[0].ServiceURL)
	assert.Equal(t, "msteams", in.approvalInputs[0].ChannelID)
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
	assert.Equal(t, []string{"Start", "PostApprovalPrompt:search", "Append:done", "End"}, in.calls)
}

func TestRouterWorkflow_StreamStartFails_PostsCompleteResponse(t *testing.T) {
	handle := outbound.TurnHandle{}
	out := &fakeOutbound{
		startResult: outbound.StartResult{Handle: &handle},
		pollResults: []outbound.PollResult{
			{Deltas: []outbound.Delta{{Text: "partial "}}},
			{Deltas: []outbound.Delta{{Text: "answer", IsFinal: true}}},
		},
	}
	in := &fakeInbound{streamStartErr: assert.AnError}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"Start", "PostMessage:partial answer"}, in.calls)
	assert.Equal(t, 2, out.pollCalls)
	assert.Empty(t, in.updateInputs)
	assert.Empty(t, in.finishInputs)
}

func TestRouterWorkflow_FinalOnlyDelta_DoesNotSendEmptyUpdate(t *testing.T) {
	handle := outbound.TurnHandle{}
	out := &fakeOutbound{
		startResult: outbound.StartResult{Handle: &handle},
		pollResults: []outbound.PollResult{
			{Deltas: []outbound.Delta{{IsFinal: true}}},
		},
	}
	in := &fakeInbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"Start", "End"}, in.calls)
	assert.Empty(t, in.updateInputs)
}

func TestRouterWorkflow_ClosedTurn_FinishesEagerlyStartedStream(t *testing.T) {
	handle := outbound.TurnHandle{}
	out := &fakeOutbound{
		startResult: outbound.StartResult{Handle: &handle},
		pollResults: []outbound.PollResult{{Closed: true}},
	}
	in := &fakeInbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"Start", "End"}, in.calls)
	require.Len(t, in.finishInputs, 1)
	assert.Empty(t, in.finishInputs[0].FullText)
}

func TestRouterWorkflow_TeamsStreamsDeltasAndFinishesWithFullText(t *testing.T) {
	handle := outbound.TurnHandle{TurnID: "turn-1", TurnNumber: 1}
	out := &fakeOutbound{
		startResult: outbound.StartResult{Handle: &handle},
		pollResults: []outbound.PollResult{{Deltas: []outbound.Delta{
			{Text: "hello "},
			{Text: "world", IsFinal: true},
		}}},
	}
	in := &fakeInbound{streamHandle: &inbound.StreamHandle{
		ID:        "teams-stream-1",
		TaskQueue: "teams-worker-1",
	}}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, wire.Input{
		Identity:  "default",
		SessionID: "teams:conversation-1",
		Message: &wire.IncomingMessage{
			MessageID:        "message-1",
			Text:             "question",
			ConversationType: "personal",
			ServiceURL:       "https://example.test/teams/",
			ChannelID:        "msteams",
		},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	require.Len(t, in.beginInputs, 1)
	assert.Equal(t, "personal", in.beginInputs[0].ConversationType)
	assert.Equal(t, "https://example.test/teams/", in.beginInputs[0].ServiceURL)
	require.Len(t, in.updateInputs, 2)
	assert.Equal(t, "hello ", in.updateInputs[0].Delta)
	assert.Equal(t, "hello ", in.updateInputs[0].FullText)
	assert.Equal(t, "world", in.updateInputs[1].Delta)
	assert.Equal(t, "hello world", in.updateInputs[1].FullText)
	require.Len(t, in.finishInputs, 1)
	assert.Equal(t, "hello world", in.finishInputs[0].FullText)
}

func TestRouterWorkflow_ContinuesLiveUpdatesAfterFailureAndFinishesWithFullText(t *testing.T) {
	handle := outbound.TurnHandle{TurnID: "turn-1"}
	out := &fakeOutbound{
		startResult: outbound.StartResult{Handle: &handle},
		pollResults: []outbound.PollResult{{Deltas: []outbound.Delta{
			{Text: "first "},
			{Text: "second "},
			{Text: "third", IsFinal: true},
		}}},
	}
	in := &fakeInbound{streamUpdateErr: assert.AnError}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"Start", "Append:first ", "Append:second ", "Append:third", "End"}, in.calls)
	require.Len(t, in.updateInputs, 3)
	require.Len(t, in.finishInputs, 1)
	assert.Equal(t, "first second third", in.finishInputs[0].FullText)
}

func TestRouterWorkflow_TeamsClosesStreamAtApprovalBoundary(t *testing.T) {
	handle := outbound.TurnHandle{TurnID: "turn-1", TurnNumber: 1}
	out := &fakeOutbound{
		startResult: outbound.StartResult{Handle: &handle},
		pollResults: []outbound.PollResult{{Deltas: []outbound.Delta{
			{Text: "before"},
			{ApprovalRequested: &outbound.ApprovalRequest{ToolID: "tool-1", ToolName: "deploy"}},
			{Text: "after", IsFinal: true},
		}}},
	}
	in := &fakeInbound{streamHandle: &inbound.StreamHandle{
		ID:                  "teams-stream-1",
		CloseBeforeApproval: true,
	}}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, wire.Input{
		Identity:  "default",
		SessionID: "teams:conversation-1",
		Message:   &wire.IncomingMessage{MessageID: "message-1", Text: "question"},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{
		"Start", "Append:before", "End", "PostApprovalPrompt:deploy",
		"Start", "Append:after", "End",
	}, in.calls)
	require.Len(t, in.finishInputs, 2)
	assert.Equal(t, "before", in.finishInputs[0].FullText)
	assert.Equal(t, "after", in.finishInputs[1].FullText)
}
