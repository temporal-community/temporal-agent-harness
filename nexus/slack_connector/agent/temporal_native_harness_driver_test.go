package agent

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"testing"

	"github.com/nexus-rpc/sdk-go/nexus"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/mock"
	"github.com/stretchr/testify/require"
	agentgen "github.com/temporalio/nexus_connector_slack/agent/generated"
	ncmsg "github.com/temporalio/nexus_connector_slack/messaging"
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
		{"tool_end", turnEvent{Type: "tool_end"}, " ✅\n", false, false},
		{"tool_error", turnEvent{Type: "tool_error", Message: "oops"}, " ❌ Error: oops_\n", false, false},
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
		func(ctx context.Context, input ncmsg.StreamInput) (string, error) { return "stream-1", nil },
		activity.RegisterOptions{Name: ncmsg.StreamActivity},
	)
	env.RegisterActivityWithOptions(
		func(ctx context.Context, input ncmsg.TextMetadata) error { return nil },
		activity.RegisterOptions{Name: ncmsg.PostMessageActivity},
	)
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
	env.OnActivity(ncmsg.StreamActivity, mock.Anything, mock.Anything).
		Run(func(args mock.Arguments) {
			in := args.Get(1).(ncmsg.StreamInput)
			if in.DeltaType == ncmsg.DeltaTypeAppend {
				appendedTexts = append(appendedTexts, in.Text)
			}
		}).Return("stream-1", nil)
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
	env.OnActivity(ncmsg.StreamActivity, mock.Anything, mock.Anything).
		Run(func(args mock.Arguments) {
			in := args.Get(1).(ncmsg.StreamInput)
			if in.DeltaType == ncmsg.DeltaTypeAppend {
				appendedTexts = append(appendedTexts, in.Text)
			}
		}).Return("stream-1", nil)
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
	env.OnActivity(ncmsg.StreamActivity, mock.Anything, mock.Anything).
		Run(func(args mock.Arguments) {
			in := args.Get(1).(ncmsg.StreamInput)
			switch in.DeltaType {
			case ncmsg.DeltaTypeStart:
				calls = append(calls, "Start")
			case ncmsg.DeltaTypeEnd:
				calls = append(calls, "Stop")
			default:
				calls = append(calls, "Append")
			}
		}).
		Return("stream-1", nil)
	env.OnActivity(ncmsg.PostMessageActivity, mock.Anything, mock.Anything).Return(nil).Maybe()

	env.ExecuteWorkflow(runDriverWorkflow, defaultInput())

	require.True(t, env.IsWorkflowCompleted())
	assert.Equal(t, []string{"Start", "Append", "Append", "Stop"}, calls)
}

func TestDriverWorkflow_StreamStartFails_FallsBackToPostMessage(t *testing.T) {
	items := []agentgen.ItemElement{
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Timestamp: 1.0, Event: turnEvent{Type: "reply_delta", Text: "partial"}}, 0, turnEventsTopic),
	}

	env := newTestEnv(t, fakeAgentService(t, items))
	registerStubActivities(env)
	var postMessageCalled bool
	env.OnActivity(ncmsg.StreamActivity, mock.Anything, mock.Anything).Return("", assert.AnError)
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
