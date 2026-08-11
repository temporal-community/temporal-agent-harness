package router

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
)

// fakeBackend is a minimal BackendDriver test double: a canned StartTurn result,
// plus one PollResult per call to PollTurn (Closed thereafter).
type fakeBackend struct {
	startResult StartResult
	startErr    error
	pollResults []PollResult
	pollErr     error
	pollCalls   int
}

func (f *fakeBackend) StartTurn(ctx workflow.Context, input Input) (StartResult, error) {
	return f.startResult, f.startErr
}

func (f *fakeBackend) PollTurn(ctx workflow.Context, handle TurnHandle, cursor int64) (PollResult, error) {
	if f.pollCalls >= len(f.pollResults) {
		if f.pollErr != nil {
			return PollResult{}, f.pollErr
		}
		return PollResult{Closed: true}, nil
	}
	res := f.pollResults[f.pollCalls]
	f.pollCalls++
	return res, nil
}

// fakeOutbound is a minimal OutboundDriver test double that records calls in order.
type fakeOutbound struct {
	calls             []string
	supportsStreaming *bool
	streamStartErr    error
	streamUpdateErr   error
	streamHandle      *StreamHandle
	beginInputs       []BeginStreamInput
	updateInputs      []UpdateStreamInput
	finishInputs      []FinishStreamInput
	postMessageInputs []TextMetadata
	approvalInputs    []ApprovalAcknowledgementInput
}

func (f *fakeOutbound) SupportsStreaming(Input) bool {
	if f.supportsStreaming == nil {
		return true
	}
	return *f.supportsStreaming
}

func (f *fakeOutbound) BeginStream(ctx workflow.Context, input BeginStreamInput) (StreamHandle, error) {
	f.calls = append(f.calls, "Start")
	f.beginInputs = append(f.beginInputs, input)
	if f.streamStartErr != nil {
		return StreamHandle{}, f.streamStartErr
	}
	if f.streamHandle != nil {
		handle := *f.streamHandle
		handle.SessionID = input.SessionID
		return handle, nil
	}
	return StreamHandle{
		ID:        "stream-1",
		SessionID: input.SessionID,
	}, nil
}

func (f *fakeOutbound) UpdateStream(ctx workflow.Context, input UpdateStreamInput) error {
	f.calls = append(f.calls, "Append:"+input.Delta)
	f.updateInputs = append(f.updateInputs, input)
	return f.streamUpdateErr
}

func (f *fakeOutbound) FinishStream(ctx workflow.Context, input FinishStreamInput) error {
	f.calls = append(f.calls, "End")
	f.finishInputs = append(f.finishInputs, input)
	return nil
}

func (f *fakeOutbound) PostMessage(ctx workflow.Context, input TextMetadata) error {
	f.calls = append(f.calls, "PostMessage:"+input.Text)
	f.postMessageInputs = append(f.postMessageInputs, input)
	return nil
}

func (f *fakeOutbound) PostApprovalPrompt(ctx workflow.Context, input ApprovalPromptInput) error {
	f.calls = append(f.calls, "PostApprovalPrompt:"+input.ToolName)
	return nil
}

func (f *fakeOutbound) AcknowledgeApproval(ctx workflow.Context, input ApprovalAcknowledgementInput) error {
	f.calls = append(f.calls, "AcknowledgeApproval:"+input.ToolName)
	f.approvalInputs = append(f.approvalInputs, input)
	return nil
}

func defaultInput() Input {
	return Input{
		Identity:  "default",
		SessionID: "slack:C12345",
		Message:   &IncomingMessage{MessageID: "m1", Text: "hello"},
	}
}

func teamsMessageInput(conversationType string) Input {
	return Input{
		Identity:  "default",
		SessionID: "teams:conversation-1",
		Message: &IncomingMessage{
			MessageID:        "message-1",
			Text:             "question",
			ConversationType: conversationType,
			ServiceURL:       "https://example.test/teams/",
			ChannelID:        "msteams",
		},
	}
}

func nonStreamingOutbound() *fakeOutbound {
	supportsStreaming := false
	return &fakeOutbound{supportsStreaming: &supportsStreaming}
}

func newTestEnv(t *testing.T, w *RouterWorkflow) *testsuite.TestWorkflowEnvironment {
	t.Helper()
	s := testsuite.WorkflowTestSuite{}
	env := s.NewTestWorkflowEnvironment()
	env.RegisterWorkflow(w.Run)
	return env
}

func TestRouterWorkflow_MessageTurn_StreamsDeltas(t *testing.T) {
	handle := TurnHandle{TurnNumber: 1}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{{Text: "hello "}, {Text: "world", IsFinal: true}}},
		},
	}
	in := &fakeOutbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"Start", "Append:hello ", "Append:world", "End"}, in.calls)
}

func TestRouterWorkflow_AccumulatesCitationsForFinishStream(t *testing.T) {
	handle := TurnHandle{TurnNumber: 1}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{
				{Text: "hello "},
				{Citations: []Citation{{URL: "https://example.com/doc", Title: "Doc", EndIndex: 3}}},
				{Text: "world", IsFinal: true},
			}},
		},
	}
	in := &fakeOutbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	require.Len(t, in.finishInputs, 1)
	assert.Equal(t, "hello world", in.finishInputs[0].Text)
	assert.Equal(t, []Citation{{URL: "https://example.com/doc", Title: "Doc", EndIndex: 3}}, in.finishInputs[0].Citations)
	// A citation-only delta (no text) doesn't trigger a live append - Slack's
	// append-only stream can't position it until FinishStream sees the full text.
	assert.Equal(t, []string{"Start", "Append:hello ", "Append:world", "End"}, in.calls)
}

func TestRouterWorkflow_ForwardsToolStatusAndThoughtSummaryVerbatim(t *testing.T) {
	handle := TurnHandle{TurnNumber: 1}
	toolStatus := &ToolStatus{ToolID: "t1", ToolName: "search", Status: ToolStarted}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{
				{ToolStatus: toolStatus},
				{ThoughtSummary: "thinking..."},
				{Text: "answer", IsFinal: true},
			}},
		},
	}
	in := &fakeOutbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	require.Len(t, in.updateInputs, 3)
	// router doesn't interpret ToolStatus/ThoughtSummary, it just forwards them -
	// rendering is entirely the outbound driver's decision.
	assert.Same(t, toolStatus, in.updateInputs[0].ToolStatus)
	assert.Empty(t, in.updateInputs[0].Delta)
	assert.Equal(t, "thinking...", in.updateInputs[1].ThoughtSummary)
	assert.Nil(t, in.updateInputs[1].ToolStatus)
	assert.Equal(t, "answer", in.updateInputs[2].Delta)
}

func TestRouterWorkflow_PostResp_PassesSegmentsToPostMessage(t *testing.T) {
	handle := TurnHandle{}
	toolStatus := &ToolStatus{ToolID: "t1", ToolName: "search", Status: ToolStarted}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{
				{ToolStatus: toolStatus},
				{Text: "answer", IsFinal: true},
			}},
		},
	}
	in := nonStreamingOutbound()

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, teamsMessageInput("channel"))

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	require.Len(t, in.postMessageInputs, 1)
	assert.Equal(t, "answer", in.postMessageInputs[0].Text)
	require.Len(t, in.postMessageInputs[0].Segments, 2)
	assert.Same(t, toolStatus, in.postMessageInputs[0].Segments[0].ToolStatus)
	assert.Equal(t, "answer", in.postMessageInputs[0].Segments[1].Text)
}

func TestRouterWorkflow_ApprovalBoundary_ResetsSegmentCitations(t *testing.T) {
	handle := TurnHandle{}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{
				{Text: "before"},
				{Citations: []Citation{{URL: "https://example.com/before", EndIndex: 1}}},
				{ApprovalRequested: &ApprovalRequest{ToolID: "tool-1", ToolName: "deploy"}},
			}},
			{Deltas: []Delta{
				{Text: "after"},
				{Citations: []Citation{{URL: "https://example.com/after", EndIndex: 1}}, IsFinal: true},
			}},
		},
	}
	streamHandle := StreamHandle{CloseBeforeApproval: true}
	in := &fakeOutbound{streamHandle: &streamHandle}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	require.Len(t, in.finishInputs, 2)
	assert.Equal(t, "before", in.finishInputs[0].Text)
	assert.Equal(t, []Citation{{URL: "https://example.com/before", EndIndex: 1}}, in.finishInputs[0].Citations)
	assert.Equal(t, "after", in.finishInputs[1].Text)
	assert.Equal(t, []Citation{{URL: "https://example.com/after", EndIndex: 1}}, in.finishInputs[1].Citations)
}

func TestRouterWorkflow_TeamsSharedConversationPostsCompleteResponse(t *testing.T) {
	for _, conversationType := range []string{"channel", "groupChat"} {
		t.Run(conversationType, func(t *testing.T) {
			handle := TurnHandle{}
			out := &fakeBackend{
				startResult: StartResult{Handle: &handle},
				pollResults: []PollResult{
					{Deltas: []Delta{{Text: "partial "}}},
					{Deltas: []Delta{{Text: "answer", IsFinal: true}}},
				},
			}
			in := nonStreamingOutbound()

			w := NewRouterWorkflow(in, out)
			env := newTestEnv(t, w)
			env.ExecuteWorkflow(w.Run, teamsMessageInput(conversationType))

			require.True(t, env.IsWorkflowCompleted())
			require.NoError(t, env.GetWorkflowError())
			assert.Equal(t, []string{"PostMessage:partial answer"}, in.calls)
			assert.Equal(t, 2, out.pollCalls)
			assert.Empty(t, in.beginInputs)
			assert.Empty(t, in.updateInputs)
			assert.Empty(t, in.finishInputs)
		})
	}
}

func TestRouterWorkflow_TeamsSharedConversationPostsApprovalBeforeCompleteResponse(t *testing.T) {
	handle := TurnHandle{}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{
				{Text: "before"},
				{ApprovalRequested: &ApprovalRequest{ToolID: "tool-1", ToolName: "deploy"}},
			}},
			{Deltas: []Delta{{Text: "after", IsFinal: true}}},
		},
	}
	in := nonStreamingOutbound()

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, teamsMessageInput("channel"))

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"PostApprovalPrompt:deploy", "PostMessage:beforeafter"}, in.calls)
	assert.Empty(t, in.beginInputs)
}

func TestRouterWorkflow_TeamsSharedConversationClosedPostsCollectedResponse(t *testing.T) {
	handle := TurnHandle{}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{{Text: "complete"}}},
			{Closed: true},
		},
	}
	in := nonStreamingOutbound()

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, teamsMessageInput("groupChat"))

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"PostMessage:complete"}, in.calls)
}

// TestRouterWorkflow_TeamsSharedConversationPostsCitationOnlyResponse guards against a
// citation-only delta (no Text/ToolStatus/ThoughtSummary) being mistaken for no content
// and dropped, e.g. an annotation for text delivered in an earlier delta.
func TestRouterWorkflow_TeamsSharedConversationPostsCitationOnlyResponse(t *testing.T) {
	handle := TurnHandle{}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{
				{Citations: []Citation{{URL: "https://example.com/doc", EndIndex: 0}}, IsFinal: true},
			}},
		},
	}
	in := nonStreamingOutbound()

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, teamsMessageInput("channel"))

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	require.Len(t, in.postMessageInputs, 1)
	assert.Equal(t, []Citation{{URL: "https://example.com/doc", EndIndex: 0}}, in.postMessageInputs[0].Citations)
}

func TestRouterWorkflow_TeamsSharedConversationDoesNotPostEmptyResponse(t *testing.T) {
	handle := TurnHandle{}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{{IsFinal: true}}},
		},
	}
	in := nonStreamingOutbound()

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, teamsMessageInput("channel"))

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Empty(t, in.calls)
}

func TestRouterWorkflow_TeamsSharedConversationDoesNotPostPartialResponseAfterPollFailure(t *testing.T) {
	handle := TurnHandle{}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{{Text: "partial"}}},
		},
		pollErr: assert.AnError,
	}
	in := nonStreamingOutbound()

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, teamsMessageInput("groupChat"))

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Empty(t, in.calls)
}

func TestRouterWorkflow_NonTeamsChannelStillStreams(t *testing.T) {
	handle := TurnHandle{}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{{Text: "answer", IsFinal: true}}},
		},
	}
	in := &fakeOutbound{}
	input := defaultInput()
	input.Message.ConversationType = "channel"

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, input)

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"Start", "Append:answer", "End"}, in.calls)
}

func TestRouterWorkflow_SynchronousReply_PostsMessageWithoutPolling(t *testing.T) {
	out := &fakeBackend{startResult: StartResult{Reply: "pong"}}
	in := &fakeOutbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"PostMessage:pong"}, in.calls)
	assert.Equal(t, 0, out.pollCalls, "a synchronous reply must not poll")
}

func TestRouterWorkflow_FireAndForget_DoesNothingFurther(t *testing.T) {
	out := &fakeBackend{startResult: StartResult{}}
	in := &fakeOutbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, Input{
		Identity:  "default",
		SessionID: "slack:C12345",
		Slash:     &SlashCommand{Name: "noop"},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Empty(t, in.calls)
	assert.Equal(t, 0, out.pollCalls)
}

func TestRouterWorkflow_ApprovalAcknowledgesOutboundDriver(t *testing.T) {
	out := &fakeBackend{startResult: StartResult{}}
	in := &fakeOutbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, Input{
		Identity:  "default",
		SessionID: "teams:conversation-1",
		Approval: &ApprovalDecision{
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
	handle := TurnHandle{}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{
				{ApprovalRequested: &ApprovalRequest{ToolID: "t1", ToolName: "search"}},
				{Text: "done", IsFinal: true},
			}},
		},
	}
	in := &fakeOutbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"Start", "PostApprovalPrompt:search", "Append:done", "End"}, in.calls)
}

func TestRouterWorkflow_StreamStartFails_FallsBackToPostMessage(t *testing.T) {
	handle := TurnHandle{}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{{Text: "partial "}}},
			{Deltas: []Delta{{Text: "answer", IsFinal: true}}},
		},
	}
	in := &fakeOutbound{streamStartErr: assert.AnError}

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
	handle := TurnHandle{}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{
			{Deltas: []Delta{{IsFinal: true}}},
		},
	}
	in := &fakeOutbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"Start", "End"}, in.calls)
	assert.Empty(t, in.updateInputs)
}

func TestRouterWorkflow_ClosedTurn_FinishesEagerlyStartedStream(t *testing.T) {
	handle := TurnHandle{}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{{Closed: true}},
	}
	in := &fakeOutbound{}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"Start", "End"}, in.calls)
	require.Len(t, in.finishInputs, 1)
}

func TestRouterWorkflow_TeamsPersonalStreamsDeltas(t *testing.T) {
	handle := TurnHandle{TurnID: "turn-1", TurnNumber: 1}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{{Deltas: []Delta{
			{Text: "hello "},
			{Text: "world", IsFinal: true},
		}}},
	}
	in := &fakeOutbound{streamHandle: &StreamHandle{
		ID:        "teams-stream-1",
		TaskQueue: "teams-worker-1",
	}}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, teamsMessageInput("personal"))

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	require.Len(t, in.beginInputs, 1)
	assert.Equal(t, "personal", in.beginInputs[0].ConversationType)
	assert.Equal(t, "https://example.test/teams/", in.beginInputs[0].ServiceURL)
	require.Len(t, in.updateInputs, 2)
	assert.Equal(t, "hello ", in.updateInputs[0].Delta)
	assert.Equal(t, "world", in.updateInputs[1].Delta)
	require.Len(t, in.finishInputs, 1)
}

func TestRouterWorkflow_ContinuesLiveUpdatesAfterFailureAndFinishes(t *testing.T) {
	handle := TurnHandle{TurnID: "turn-1"}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{{Deltas: []Delta{
			{Text: "first "},
			{Text: "second "},
			{Text: "third", IsFinal: true},
		}}},
	}
	in := &fakeOutbound{streamUpdateErr: assert.AnError}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{"Start", "Append:first ", "Append:second ", "Append:third", "End"}, in.calls)
	require.Len(t, in.updateInputs, 3)
	require.Len(t, in.finishInputs, 1)
}

func TestRouterWorkflow_TeamsClosesStreamAtApprovalBoundary(t *testing.T) {
	handle := TurnHandle{TurnID: "turn-1", TurnNumber: 1}
	out := &fakeBackend{
		startResult: StartResult{Handle: &handle},
		pollResults: []PollResult{{Deltas: []Delta{
			{Text: "before"},
			{ApprovalRequested: &ApprovalRequest{ToolID: "tool-1", ToolName: "deploy"}},
			{Text: "after", IsFinal: true},
		}}},
	}
	in := &fakeOutbound{streamHandle: &StreamHandle{
		ID:                  "teams-stream-1",
		CloseBeforeApproval: true,
	}}

	w := NewRouterWorkflow(in, out)
	env := newTestEnv(t, w)
	env.ExecuteWorkflow(w.Run, Input{
		Identity:  "default",
		SessionID: "teams:conversation-1",
		Message:   &IncomingMessage{MessageID: "message-1", Text: "question"},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, []string{
		"Start", "Append:before", "End", "PostApprovalPrompt:deploy",
		"Start", "Append:after", "End",
	}, in.calls)
	require.Len(t, in.finishInputs, 2)
}
