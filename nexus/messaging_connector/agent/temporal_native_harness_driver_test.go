package agent

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/nexus-rpc/sdk-go/nexus"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/mock"
	"github.com/stretchr/testify/require"
	agentgen "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/agent/generated"
	ncmsg "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging"
	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"
)

// runDriverWorkflow is a package-level wrapper used by the test environment so the
// TemporalNativeHarnessDriver can be exercised through a registered workflow function.
func runDriverWorkflow(ctx workflow.Context, input ConnectorWorkflowInput) error {
	d := &TemporalNativeHarnessDriver{}
	handle, err := d.ReceiveMessageFromPlatform(ctx, input)
	if err != nil {
		return nil
	}
	return d.RespondToPlatform(ctx, handle, input)
}

// -- Unit tests for pure helper functions ------------------------------------

func TestTurnEventToDelta(t *testing.T) {
	cases := []struct {
		name      string
		event     turnEvent
		wantText  string
		wantFinal bool
		wantNil   bool
	}{
		{"reply_delta", turnEvent{Type: "reply_delta", Text: "hello"}, "hello", false, false},
		{"reply", turnEvent{Type: "reply", Text: "full text"}, "", true, false},
		{"tool_start", turnEvent{Type: "tool_start", ToolName: "search"}, "\n_search..._", false, false},
		{"tool_end", turnEvent{Type: "tool_end"}, " ✅\n\n", false, false},
		{"tool_error", turnEvent{Type: "tool_error", Message: "oops"}, "\n❌ Error: oops\n\n", false, false},
		{"error", turnEvent{Type: "error", Message: "crash"}, "[error] crash", true, false},
		{"thought_summary with text", turnEvent{Type: "thought_summary", Delta: map[string]any{"text": "thinking..."}}, "thinking...", false, false},
		{"thought_summary empty text", turnEvent{Type: "thought_summary", Delta: map[string]any{"text": ""}}, "", false, true},
		{"unknown type", turnEvent{Type: "unknown_event"}, "", false, true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := turnEventToDelta(tc.event)
			if tc.wantNil {
				assert.Nil(t, d)
				return
			}
			require.NotNil(t, d)
			assert.Equal(t, tc.wantText, d.Text)
			assert.Equal(t, tc.wantFinal, d.IsFinal)
		})
	}
}

func TestReplyEventDoesNotAppendText(t *testing.T) {
	d := turnEventToDelta(turnEvent{Type: "reply", Text: "full response"})
	require.NotNil(t, d)
	assert.Empty(t, d.Text, "reply event should not contribute text to avoid duplicate in stream")
	assert.True(t, d.IsFinal)
}

func TestDecodeTurnEvent_RoundTrip(t *testing.T) {
	si := streamItem{
		TurnID:     "t1",
		TurnNumber: 3,
		Timestamp:  1700000000.0,
		Event:      turnEvent{Type: "reply_delta", Text: "hello"},
	}
	item := makeTestStreamItem(t, si, 0, turnEventsTopic)

	turnNumber, got, err := decodeTurnEvent(item)
	require.NoError(t, err)
	assert.Equal(t, 3, turnNumber)
	assert.Equal(t, "reply_delta", got.Type)
	assert.Equal(t, "hello", got.Text)
}

// -- Workflow tests ----------------------------------------------------------

// fakeAgentService returns a nexus.Service that immediately completes operations
// with canned responses. PollMessages returns the given items on the first call,
// then Closed=true on subsequent calls so the driver exits cleanly.
func fakeAgentService(t *testing.T, items []agentgen.ItemElement) *nexus.Service {
	t.Helper()
	svc := nexus.NewService(agentgen.AgentService.ServiceName)
	svc.MustRegister(nexus.NewSyncOperation(
		agentgen.AgentService.SendAgentMessage.Name(),
		func(ctx context.Context, input agentgen.SendAgentMessageInput, opts nexus.StartOperationOptions) (agentgen.SendMessageOutput, error) {
			return agentgen.SendMessageOutput{TurnNumber: 1, TurnID: "turn-1"}, nil
		},
	))
	called := false
	svc.MustRegister(nexus.NewSyncOperation(
		agentgen.AgentService.PollMessages.Name(),
		func(ctx context.Context, input agentgen.PollMessagesInput, opts nexus.StartOperationOptions) (agentgen.PollMessagesOutput, error) {
			if !called && len(items) > 0 {
				called = true
				return agentgen.PollMessagesOutput{Items: items, NextOffset: int64(len(items))}, nil
			}
			return agentgen.PollMessagesOutput{Closed: true, NextOffset: input.Cursor}, nil
		},
	))
	return svc
}

// registerStubActivities registers no-op stubs under the activity names used by the
// driver. Required by the test environment before OnActivity mocks can be set.
func registerStubActivities(env *testsuite.TestWorkflowEnvironment) {
	env.RegisterActivityWithOptions(
		func(ctx context.Context, input ncmsg.BeginStreamInput) (ncmsg.StreamHandle, error) {
			return testStreamHandle(input.SessionID), nil
		},
		activity.RegisterOptions{Name: ncmsg.BeginStreamActivity},
	)
	env.RegisterActivityWithOptions(
		func(ctx context.Context, input ncmsg.UpdateStreamInput) error { return nil },
		activity.RegisterOptions{Name: ncmsg.UpdateStreamActivity},
	)
	env.RegisterActivityWithOptions(
		func(ctx context.Context, input ncmsg.FinishStreamInput) error { return nil },
		activity.RegisterOptions{Name: ncmsg.FinishStreamActivity},
	)
	env.RegisterActivityWithOptions(
		func(ctx context.Context, input ncmsg.TextMetadata) error { return nil },
		activity.RegisterOptions{Name: ncmsg.PostMessageActivity},
	)
	env.RegisterActivityWithOptions(
		func(ctx context.Context, input ncmsg.ApprovalPromptInput) error { return nil },
		activity.RegisterOptions{Name: ncmsg.PostApprovalPromptActivity},
	)
}

func testStreamHandle(sessionID string) ncmsg.StreamHandle {
	handle := ncmsg.StreamHandle{
		ID:           "stream-1",
		SessionID:    sessionID,
		WireTextMode: ncmsg.StreamWireTextDelta,
	}
	if strings.HasPrefix(sessionID, "teams:") {
		handle.WireTextMode = ncmsg.StreamWireTextFullText
		handle.CloseBeforeApproval = true
		handle.NextSequence = 2
	}
	return handle
}

func newTestEnv(t *testing.T, svc *nexus.Service) *testsuite.TestWorkflowEnvironment {
	t.Helper()
	s := testsuite.WorkflowTestSuite{}
	env := s.NewTestWorkflowEnvironment()
	env.RegisterWorkflow(runDriverWorkflow)
	env.RegisterNexusService(svc)
	return env
}

func defaultInput() ConnectorWorkflowInput {
	return ConnectorWorkflowInput{
		Identity:  "default",
		SessionID: "slack:C12345",
		Message:   &IncomingMessage{MessageID: "m1", Sender: "user", Text: "question", Timestamp: "1234.0"},
	}
}

// TestDriverWorkflow_StartsPollingFromStreamHeadOffset verifies that
// the first pollMessages call uses the StreamHeadOffset from sendMessage, so
// the driver never replays events that were already published before this turn.
func TestDriverWorkflow_StartsPollingFromStreamHeadOffset(t *testing.T) {
	const streamHeadOffset = int64(5)

	freshItems := []agentgen.ItemElement{
		makeTestStreamItem(t, streamItem{TurnID: "t2", TurnNumber: 2, Timestamp: 3.0, Event: turnEvent{Type: "reply_delta", Text: "fresh"}}, streamHeadOffset, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnID: "t2", TurnNumber: 2, Timestamp: 4.0, Event: turnEvent{Type: "reply"}}, streamHeadOffset+1, turnEventsTopic),
	}

	svc := nexus.NewService(agentgen.AgentService.ServiceName)
	svc.MustRegister(nexus.NewSyncOperation(
		agentgen.AgentService.SendAgentMessage.Name(),
		func(ctx context.Context, input agentgen.SendAgentMessageInput, opts nexus.StartOperationOptions) (agentgen.SendMessageOutput, error) {
			return agentgen.SendMessageOutput{TurnNumber: 2, TurnID: "t2", StreamHeadOffset: streamHeadOffset}, nil
		},
	))
	called := false
	var firstPollCursor int64
	svc.MustRegister(nexus.NewSyncOperation(
		agentgen.AgentService.PollMessages.Name(),
		func(ctx context.Context, input agentgen.PollMessagesInput, opts nexus.StartOperationOptions) (agentgen.PollMessagesOutput, error) {
			if !called {
				called = true
				firstPollCursor = input.Cursor
				return agentgen.PollMessagesOutput{Items: freshItems, NextOffset: streamHeadOffset + int64(len(freshItems))}, nil
			}
			return agentgen.PollMessagesOutput{Closed: true, NextOffset: input.Cursor}, nil
		},
	))

	env := newTestEnv(t, svc)
	registerStubActivities(env)
	var appendedTexts []string
	env.OnActivity(ncmsg.UpdateStreamActivity, mock.Anything, mock.Anything).
		Run(func(args mock.Arguments) {
			in := args.Get(1).(ncmsg.UpdateStreamInput)
			appendedTexts = append(appendedTexts, in.Delta)
		}).Return(nil)
	env.OnActivity(ncmsg.PostMessageActivity, mock.Anything, mock.Anything).Return(nil).Maybe()

	env.ExecuteWorkflow(runDriverWorkflow, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, streamHeadOffset, firstPollCursor, "first poll must start from StreamHeadOffset")
	assert.Equal(t, []string{"fresh"}, appendedTexts)
}

// TestDriverWorkflow_SkipsStaleEventsByTurnNumber verifies the secondary
// turn-number guard: even if pollMessages returns items from older turns (e.g.
// because StreamHeadOffset was 0), items whose turn_number < sendOut.TurnNumber
// are silently dropped.
func TestDriverWorkflow_SkipsStaleEventsByTurnNumber(t *testing.T) {
	items := []agentgen.ItemElement{
		makeTestStreamItem(t, streamItem{TurnID: "old", TurnNumber: 1, Timestamp: 1.0, Event: turnEvent{Type: "reply_delta", Text: "stale"}}, 0, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnID: "old", TurnNumber: 1, Timestamp: 2.0, Event: turnEvent{Type: "reply"}}, 1, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnID: "t2", TurnNumber: 2, Timestamp: 3.0, Event: turnEvent{Type: "reply_delta", Text: "fresh"}}, 2, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnID: "t2", TurnNumber: 2, Timestamp: 4.0, Event: turnEvent{Type: "reply"}}, 3, turnEventsTopic),
	}

	svc := nexus.NewService(agentgen.AgentService.ServiceName)
	svc.MustRegister(nexus.NewSyncOperation(
		agentgen.AgentService.SendAgentMessage.Name(),
		func(ctx context.Context, input agentgen.SendAgentMessageInput, opts nexus.StartOperationOptions) (agentgen.SendMessageOutput, error) {
			return agentgen.SendMessageOutput{TurnNumber: 2, TurnID: "t2"}, nil
		},
	))
	called := false
	svc.MustRegister(nexus.NewSyncOperation(
		agentgen.AgentService.PollMessages.Name(),
		func(ctx context.Context, input agentgen.PollMessagesInput, opts nexus.StartOperationOptions) (agentgen.PollMessagesOutput, error) {
			if !called {
				called = true
				return agentgen.PollMessagesOutput{Items: items, NextOffset: int64(len(items))}, nil
			}
			return agentgen.PollMessagesOutput{Closed: true, NextOffset: input.Cursor}, nil
		},
	))

	env := newTestEnv(t, svc)
	registerStubActivities(env)
	var appendedTexts []string
	env.OnActivity(ncmsg.UpdateStreamActivity, mock.Anything, mock.Anything).
		Run(func(args mock.Arguments) {
			in := args.Get(1).(ncmsg.UpdateStreamInput)
			appendedTexts = append(appendedTexts, in.Delta)
		}).Return(nil)
	env.OnActivity(ncmsg.PostMessageActivity, mock.Anything, mock.Anything).Return(nil).Maybe()

	env.ExecuteWorkflow(runDriverWorkflow, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	assert.Equal(t, []string{"fresh"}, appendedTexts, "only turn 2 reply_delta should be appended")
}

func TestDriverWorkflow_ActivitySequence(t *testing.T) {
	// A turn with two reply_delta events and a closing reply event should produce:
	// Stream(start) once, Stream(append) twice (once per delta), Stream(stop) once.
	items := []agentgen.ItemElement{
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 1.0, Event: turnEvent{Type: "reply_delta", Text: "hello "}}, 0, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 2.0, Event: turnEvent{Type: "reply_delta", Text: "world"}}, 1, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 3.0, Event: turnEvent{Type: "reply"}}, 2, turnEventsTopic),
	}

	env := newTestEnv(t, fakeAgentService(t, items))
	registerStubActivities(env)
	var calls []string
	env.OnActivity(ncmsg.BeginStreamActivity, mock.Anything, mock.Anything).
		Run(func(mock.Arguments) { calls = append(calls, "Start") }).
		Return(testStreamHandle(defaultInput().SessionID), nil)
	env.OnActivity(ncmsg.UpdateStreamActivity, mock.Anything, mock.Anything).
		Run(func(mock.Arguments) { calls = append(calls, "Append") }).
		Return(nil)
	env.OnActivity(ncmsg.FinishStreamActivity, mock.Anything, mock.Anything).
		Run(func(mock.Arguments) { calls = append(calls, "Stop") }).
		Return(nil)
	env.OnActivity(ncmsg.PostMessageActivity, mock.Anything, mock.Anything).Return(nil).Maybe()

	env.ExecuteWorkflow(runDriverWorkflow, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	assert.Equal(t, []string{"Start", "Append", "Append", "Stop"}, calls)
}

func TestDriverWorkflow_TeamsBuffersDeltasAndFinishesWithFullText(t *testing.T) {
	items := []agentgen.ItemElement{
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 1, Event: turnEvent{Type: "reply_delta", Text: "hello "}}, 0, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 2, Event: turnEvent{Type: "reply_delta", Text: "world"}}, 1, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 3, Event: turnEvent{Type: "reply"}}, 2, turnEventsTopic),
	}

	env := newTestEnv(t, fakeAgentService(t, items))
	registerStubActivities(env)
	handle := testStreamHandle("teams:conversation-1")
	handle.MinUpdateInterval = time.Hour
	env.OnActivity(ncmsg.BeginStreamActivity, mock.Anything, mock.Anything).Return(handle, nil)
	updateCalls := 0
	env.OnActivity(ncmsg.UpdateStreamActivity, mock.Anything, mock.Anything).
		Run(func(mock.Arguments) { updateCalls++ }).
		Return(nil).Maybe()
	var finished ncmsg.FinishStreamInput
	env.OnActivity(ncmsg.FinishStreamActivity, mock.Anything, mock.Anything).
		Run(func(args mock.Arguments) { finished = args.Get(1).(ncmsg.FinishStreamInput) }).
		Return(nil)
	env.OnActivity(ncmsg.PostMessageActivity, mock.Anything, mock.Anything).Return(nil).Maybe()

	input := defaultInput()
	input.SessionID = "teams:conversation-1"
	input.Message.ConversationType = "personal"
	env.ExecuteWorkflow(runDriverWorkflow, input)

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Zero(t, updateCalls)
	assert.Equal(t, "hello world", finished.FullText)
	assert.Equal(t, handle.ID, finished.Handle.ID)
	assert.Contains(t, finished.OperationID, "/turn-1/segment/0/finish/")
}

func TestDriverWorkflow_RetainsPendingDeltaAfterUpdateFailure(t *testing.T) {
	items := []agentgen.ItemElement{
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 1, Event: turnEvent{Type: "reply_delta", Text: "hello "}}, 0, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 2, Event: turnEvent{Type: "reply_delta", Text: "world"}}, 1, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 3, Event: turnEvent{Type: "reply"}}, 2, turnEventsTopic),
	}

	env := newTestEnv(t, fakeAgentService(t, items))
	registerStubActivities(env)
	env.OnActivity(ncmsg.UpdateStreamActivity, mock.Anything, mock.Anything).
		Return(assert.AnError).Once()
	var retriedDelta string
	env.OnActivity(ncmsg.UpdateStreamActivity, mock.Anything, mock.Anything).
		Run(func(args mock.Arguments) {
			retriedDelta = args.Get(1).(ncmsg.UpdateStreamInput).Delta
		}).
		Return(nil).Maybe()
	env.OnActivity(ncmsg.PostMessageActivity, mock.Anything, mock.Anything).Return(nil).Maybe()

	env.ExecuteWorkflow(runDriverWorkflow, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	assert.Equal(t, "hello world", retriedDelta)
}

func TestDriverWorkflow_ApprovalBoundaryActivitySequence(t *testing.T) {
	approval := turnEvent{
		Type:      "tool_approval_requested",
		ToolID:    "tool-1",
		ToolName:  "search",
		ToolInput: map[string]any{"query": "Temporal"},
	}

	tests := []struct {
		name             string
		sessionID        string
		conversationType string
		events           []turnEvent
		wantCalls        []string
	}{
		{
			name:             "Teams personal chat closes the active stream around approval",
			sessionID:        "teams:conversation-1",
			conversationType: "personal",
			events: []turnEvent{
				{Type: "reply_delta", Text: "before"},
				approval,
				{Type: "reply_delta", Text: "after"},
				{Type: "reply"},
			},
			wantCalls: []string{"Start", "Append", "End", "ApprovalPrompt", "Start", "Append", "End"},
		},
		{
			name:      "Slack keeps one stream across approval",
			sessionID: "slack:C12345",
			events: []turnEvent{
				{Type: "reply_delta", Text: "before"},
				approval,
				{Type: "reply_delta", Text: "after"},
				{Type: "reply"},
			},
			wantCalls: []string{"Start", "Append", "ApprovalPrompt", "Append", "End"},
		},
		{
			name:             "Teams personal approval first does not end an empty stream",
			sessionID:        "teams:conversation-1",
			conversationType: "personal",
			events: []turnEvent{
				approval,
				{Type: "reply_delta", Text: "after"},
				{Type: "reply"},
			},
			wantCalls: []string{"ApprovalPrompt", "Start", "Append", "End"},
		},
		{
			name:             "Teams channel closes the active message around approval",
			sessionID:        "teams:conversation-1",
			conversationType: "channel",
			events: []turnEvent{
				{Type: "reply_delta", Text: "before"},
				approval,
				{Type: "reply_delta", Text: "after"},
				{Type: "reply"},
			},
			wantCalls: []string{"Start", "Append", "End", "ApprovalPrompt", "Start", "Append", "End"},
		},
		{
			name:             "Teams channel approval first does not end an empty response",
			sessionID:        "teams:conversation-1",
			conversationType: "channel",
			events: []turnEvent{
				approval,
				{Type: "reply_delta", Text: "after"},
				{Type: "reply"},
			},
			wantCalls: []string{"ApprovalPrompt", "Start", "Append", "End"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			items := make([]agentgen.ItemElement, 0, len(tc.events))
			for i, event := range tc.events {
				items = append(items, makeTestStreamItem(t, streamItem{
					TurnID:     "t1",
					TurnNumber: 1,
					Timestamp:  float64(i + 1),
					Event:      event,
				}, int64(i), turnEventsTopic))
			}

			env := newTestEnv(t, fakeAgentService(t, items))
			registerStubActivities(env)
			var calls []string
			env.OnActivity(ncmsg.BeginStreamActivity, mock.Anything, mock.Anything).
				Run(func(mock.Arguments) { calls = append(calls, "Start") }).
				Return(testStreamHandle(tc.sessionID), nil)
			env.OnActivity(ncmsg.UpdateStreamActivity, mock.Anything, mock.Anything).
				Run(func(mock.Arguments) { calls = append(calls, "Append") }).
				Return(nil)
			env.OnActivity(ncmsg.FinishStreamActivity, mock.Anything, mock.Anything).
				Run(func(mock.Arguments) { calls = append(calls, "End") }).
				Return(nil)
			env.OnActivity(ncmsg.PostApprovalPromptActivity, mock.Anything, mock.Anything).
				Run(func(mock.Arguments) { calls = append(calls, "ApprovalPrompt") }).
				Return(nil)
			env.OnActivity(ncmsg.PostMessageActivity, mock.Anything, mock.Anything).Return(nil).Maybe()

			input := defaultInput()
			input.SessionID = tc.sessionID
			input.Message.ConversationType = tc.conversationType
			env.ExecuteWorkflow(runDriverWorkflow, input)

			require.True(t, env.IsWorkflowCompleted())
			require.NoError(t, env.GetWorkflowError())
			assert.Equal(t, tc.wantCalls, calls)
		})
	}
}

func TestDriverWorkflow_PropagatesTeamsConversationTypeToStreamStart(t *testing.T) {
	items := []agentgen.ItemElement{
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 1, Event: turnEvent{Type: "reply_delta", Text: "answer"}}, 0, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 2, Event: turnEvent{Type: "reply"}}, 1, turnEventsTopic),
	}

	for _, conversationType := range []string{"personal", "channel", "groupChat", ""} {
		t.Run(conversationType, func(t *testing.T) {
			env := newTestEnv(t, fakeAgentService(t, items))
			registerStubActivities(env)
			var got ncmsg.BeginStreamInput
			env.OnActivity(ncmsg.BeginStreamActivity, mock.Anything, mock.Anything).
				Run(func(args mock.Arguments) {
					got = args.Get(1).(ncmsg.BeginStreamInput)
				}).
				Return(testStreamHandle("teams:conversation-1"), nil)
			env.OnActivity(ncmsg.PostMessageActivity, mock.Anything, mock.Anything).Return(nil).Maybe()

			input := defaultInput()
			input.SessionID = "teams:conversation-1"
			input.Message.ConversationType = conversationType
			input.Message.ServiceURL = "https://example.test/teams/"
			input.Message.ChannelID = "msteams"
			env.ExecuteWorkflow(runDriverWorkflow, input)

			require.True(t, env.IsWorkflowCompleted())
			require.NoError(t, env.GetWorkflowError())
			assert.Equal(t, conversationType, got.ConversationType)
			assert.Equal(t, "https://example.test/teams/", got.ServiceURL)
			assert.Equal(t, "msteams", got.ChannelID)
		})
	}
}

func TestDriverWorkflow_SlackStreamStartFails_FallsBackToPostMessage(t *testing.T) {
	items := []agentgen.ItemElement{
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 1.0, Event: turnEvent{Type: "reply_delta", Text: "partial"}}, 0, turnEventsTopic),
	}

	env := newTestEnv(t, fakeAgentService(t, items))
	registerStubActivities(env)
	var postMessageCalled bool
	env.OnActivity(ncmsg.BeginStreamActivity, mock.Anything, mock.Anything).Return(ncmsg.StreamHandle{}, assert.AnError)
	env.OnActivity(ncmsg.PostMessageActivity, mock.Anything, mock.Anything).
		Run(func(_ mock.Arguments) { postMessageCalled = true }).
		Return(nil)

	env.ExecuteWorkflow(runDriverWorkflow, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	assert.True(t, postMessageCalled, "expected PostMessage fallback when stream start fails")
}

// -- Test helpers ------------------------------------------------------------

// makeTestStreamItem encodes a streamItem into the wire format expected by decodeTurnEvent:
// base64(proto.Marshal(Payload{encoding:"json/plain", data:<json>}))
func makeTestStreamItem(t *testing.T, si streamItem, offset int64, topic string) agentgen.ItemElement {
	t.Helper()
	data, err := json.Marshal(si)
	require.NoError(t, err)
	payload := &commonpb.Payload{
		Metadata: map[string][]byte{"encoding": []byte("json/plain")},
		Data:     data,
	}
	b, err := proto.Marshal(payload)
	require.NoError(t, err)
	return agentgen.ItemElement{
		Topic:  topic,
		Offset: offset,
		Data:   base64.StdEncoding.EncodeToString(b),
	}
}
