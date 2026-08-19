package agent

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"testing"

	"github.com/nexus-rpc/sdk-go/nexus"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	harnessgen "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/agent/generated"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"
)

// -- Unit tests for pure helper functions ------------------------------------

func TestTurnEventToDelta(t *testing.T) {
	cases := []struct {
		name      string
		event     turnEvent
		wantText  string
		wantFinal bool
	}{
		{"reply_delta", turnEvent{Type: "reply_delta", Text: "hello"}, "hello", false},
		{"reply", turnEvent{Type: "reply", Text: "full text"}, "", true},
		{"error", turnEvent{Type: "error", Message: "crash"}, "[error] crash", true},
		{"thought_summary empty text", turnEvent{Type: "thought_summary", Delta: map[string]any{"text": ""}}, "", false},
		{"unknown type", turnEvent{Type: "unknown_event"}, "", false},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := turnEventToDelta(tc.event)
			require.NotNil(t, d, "turnEventToDelta must never return nil - a full-fidelity outbound driver still needs EventType for events with no text rendering")
			assert.Equal(t, tc.event.Type, d.EventType)
			assert.Equal(t, tc.wantText, d.Text)
			assert.Equal(t, tc.wantFinal, d.IsFinal)
		})
	}
}

func TestTurnEventToDelta_ToolStatus(t *testing.T) {
	cases := []struct {
		name  string
		event turnEvent
		want  router.ToolStatus
	}{
		{
			"tool_start",
			turnEvent{Type: "tool_start", ToolID: "t1", ToolName: "search"},
			router.ToolStatus{ToolID: "t1", ToolName: "search", Status: router.ToolStarted},
		},
		{
			"tool_end",
			turnEvent{Type: "tool_end", ToolID: "t1", ToolName: "search"},
			router.ToolStatus{ToolID: "t1", ToolName: "search", Status: router.ToolCompleted},
		},
		{
			"tool_error",
			turnEvent{Type: "tool_error", ToolID: "t1", ToolName: "search", Message: "oops"},
			router.ToolStatus{ToolID: "t1", ToolName: "search", Status: router.ToolErrored, Message: "oops"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := turnEventToDelta(tc.event)
			require.NotNil(t, d)
			require.NotNil(t, d.ToolStatus)
			assert.Equal(t, tc.want, *d.ToolStatus)
			assert.Empty(t, d.Text, "tool status deltas carry no text - rendering is up to the outbound driver")
		})
	}
}

func TestTurnEventToDelta_ThoughtSummary(t *testing.T) {
	d := turnEventToDelta(turnEvent{Type: "thought_summary", Delta: map[string]any{"text": "thinking..."}})
	require.NotNil(t, d)
	assert.Equal(t, "thinking...", d.ThoughtSummary)
	assert.Empty(t, d.Text, "thought summaries are not reply text - citations must not be indexed against them")
}

func TestTurnEventToDelta_TextAnnotation_ProducesCitations(t *testing.T) {
	d := turnEventToDelta(turnEvent{
		Type: "text_annotation",
		Delta: map[string]any{
			"annotations": []any{
				map[string]any{
					"type":         "file_citation",
					"file_name":    "runbook.md",
					"document_uri": "https://example.com/runbook",
					"end_index":    float64(42),
					"custom_metadata": map[string]any{
						"deep_url": "https://example.com/runbook#section-2",
						"heading":  "Section 2",
					},
				},
				map[string]any{
					"type":      "file_citation",
					"file_name": "notes.txt",
				},
			},
		},
	})

	require.NotNil(t, d)
	assert.Empty(t, d.Text)
	require.Len(t, d.Citations, 2)
	assert.Equal(t, router.Citation{URL: "https://example.com/runbook#section-2", Title: "Section 2", EndIndex: 42}, d.Citations[0])
	assert.Equal(t, router.Citation{URL: "", Title: "notes.txt", EndIndex: -1}, d.Citations[1])
}

func TestTurnEventToDelta_TextAnnotation_NoAnnotations_ProducesEmptyCitations(t *testing.T) {
	d := turnEventToDelta(turnEvent{Type: "text_annotation", Delta: map[string]any{}})
	require.NotNil(t, d)
	assert.Equal(t, "text_annotation", d.EventType)
	assert.Empty(t, d.Citations)
}

// TestToolStatusCarriesNoText guards against tool_start/tool_end/tool_error deltas
// contributing anything to the reply text that citations get indexed against - see
// teamsoutbound.flattenSegments for where this structured status gets turned back
// into the "\n_toolName..._" ✅ text Teams shows, and slackoutbound's UpdateStream for
// how Slack renders it as a separate threaded message instead.
func TestToolStatusCarriesNoText(t *testing.T) {
	start := turnEventToDelta(turnEvent{Type: "tool_start", ToolID: "t1", ToolName: "file_search"})
	end := turnEventToDelta(turnEvent{Type: "tool_end", ToolID: "t1", ToolName: "file_search"})
	reply := turnEventToDelta(turnEvent{Type: "reply_delta", Text: "A Local Activity runs in the Workflow process."})
	require.NotNil(t, start)
	require.NotNil(t, end)
	require.NotNil(t, reply)

	assert.Empty(t, start.Text)
	assert.Empty(t, end.Text)
	assert.Equal(t, "A Local Activity runs in the Workflow process.", reply.Text)
}

func TestReplyEventDoesNotAppendText(t *testing.T) {
	d := turnEventToDelta(turnEvent{Type: "reply", Text: "full response"})
	require.NotNil(t, d)
	assert.Empty(t, d.Text, "reply event should not contribute text to avoid duplicate in stream")
	assert.True(t, d.IsFinal)
}

func TestToolApprovalRequestedEvent_ProducesApprovalRequestedDelta(t *testing.T) {
	d := turnEventToDelta(turnEvent{
		Type: "tool_approval_requested", ToolID: "t1", ToolName: "search",
		ToolInput: map[string]any{"query": "foo"},
	})
	require.NotNil(t, d)
	require.NotNil(t, d.ApprovalRequested)
	assert.Equal(t, "t1", d.ApprovalRequested.ToolID)
	assert.Equal(t, "search", d.ApprovalRequested.ToolName)
	assert.JSONEq(t, `{"query":"foo"}`, d.ApprovalRequested.ToolInputJSON)
}

func TestDecodeTurnEvent_RoundTrip(t *testing.T) {
	si := streamItem{
		AgentID:    "agent-1",
		TurnID:     "t1",
		TurnNumber: 3,
		Timestamp:  1700000000.0,
		Event:      turnEvent{Type: "reply_delta", Text: "hello"},
	}
	item := makeTestStreamItem(t, si, 42, turnEventsTopic)

	got, merged, err := decodeTurnEvent(item)
	require.NoError(t, err)
	assert.Equal(t, 3, got.TurnNumber)
	assert.Equal(t, "reply_delta", got.Event.Type)
	assert.Equal(t, "hello", got.Event.Text)

	var mergedFields map[string]any
	require.NoError(t, json.Unmarshal(merged, &mergedFields))
	assert.Equal(t, "reply_delta", mergedFields["type"])
	assert.Equal(t, "hello", mergedFields["text"])
	assert.Equal(t, "agent-1", mergedFields["agent_id"])
	assert.Equal(t, "t1", mergedFields["turn_id"])
	assert.Equal(t, float64(3), mergedFields["turn_number"])
	assert.Equal(t, 1700000000.0, mergedFields["timestamp"])
	assert.Equal(t, float64(42), mergedFields["resume_offset"])
}

// -- Driver tests -------------------------------------------------------------

// runStartTurnWorkflow and runPollTurnWorkflow are package-level wrappers so Driver's
// workflow.Context methods can be exercised through the Temporal test environment.
func runStartTurnWorkflow(ctx workflow.Context, input router.Input) (router.StartResult, error) {
	d := &Driver{}
	return d.StartTurn(ctx, input)
}

type pollTurnWorkflowInput struct {
	Handle router.TurnHandle
	Cursor int64
}

func runPollTurnWorkflow(ctx workflow.Context, in pollTurnWorkflowInput) (router.PollResult, error) {
	d := &Driver{}
	return d.PollTurn(ctx, in.Handle, in.Cursor)
}

func newTestEnv(t *testing.T, svc *nexus.Service) *testsuite.TestWorkflowEnvironment {
	t.Helper()
	s := testsuite.WorkflowTestSuite{}
	env := s.NewTestWorkflowEnvironment()
	env.RegisterWorkflow(runStartTurnWorkflow)
	env.RegisterWorkflow(runPollTurnWorkflow)
	env.RegisterNexusService(svc)
	return env
}

func TestStartTurn_Message_ReturnsHandleFromSendAgentMessage(t *testing.T) {
	svc := nexus.NewService(harnessgen.AgentService.ServiceName)
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.SendAgentMessage.Name(),
		func(ctx context.Context, input harnessgen.SendAgentMessageInput, opts nexus.StartOperationOptions) (harnessgen.SendMessageOutput, error) {
			assert.Equal(t, "ask", input.MsgType)
			return harnessgen.SendMessageOutput{TurnNumber: 2, StreamHeadOffset: ptr(int64(5))}, nil
		},
	))

	env := newTestEnv(t, svc)
	env.ExecuteWorkflow(runStartTurnWorkflow, router.Input{
		SessionID: "slack:C1",
		Message:   &router.IncomingMessage{Text: "hi"},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	var result router.StartResult
	require.NoError(t, env.GetWorkflowResult(&result))
	require.NotNil(t, result.Handle)
	assert.Equal(t, int64(2), result.Handle.TurnNumber)
	assert.Equal(t, int64(5), result.Handle.StreamHeadOffset)
	assert.Equal(t, "slack:C1", result.Handle.SessionID)
}

func TestStartTurn_Slash_HarnessCommand_ReturnsSynchronousReply(t *testing.T) {
	svc := nexus.NewService(harnessgen.AgentService.ServiceName)
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.QueryOperatorInterface.Name(),
		func(ctx context.Context, input harnessgen.QuerySessionInput, opts nexus.StartOperationOptions) (harnessgen.QueryOperatorInterfaceOutput, error) {
			return harnessgen.QueryOperatorInterfaceOutput{Commands: []harnessgen.OperatorCommand{
				{Name: "stop", Source: "harness"},
			}}, nil
		},
	))
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.ExecuteOperatorCommand.Name(),
		func(ctx context.Context, input harnessgen.ExecuteOperatorCommandInput, opts nexus.StartOperationOptions) (harnessgen.ExecuteOperatorCommandOutput, error) {
			assert.Equal(t, "stop", input.Name)
			return harnessgen.ExecuteOperatorCommandOutput{Reply: "Stopped."}, nil
		},
	))

	env := newTestEnv(t, svc)
	env.ExecuteWorkflow(runStartTurnWorkflow, router.Input{
		SessionID: "slack:C1",
		Slash:     &router.SlashCommand{Name: "stop"},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	var result router.StartResult
	require.NoError(t, env.GetWorkflowResult(&result))
	assert.Equal(t, "Stopped.", result.Reply)
	assert.Nil(t, result.Handle)
}

func TestStartTurn_Slash_AgentOwned_CreatesTurn(t *testing.T) {
	svc := nexus.NewService(harnessgen.AgentService.ServiceName)
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.QueryOperatorInterface.Name(),
		func(ctx context.Context, input harnessgen.QuerySessionInput, opts nexus.StartOperationOptions) (harnessgen.QueryOperatorInterfaceOutput, error) {
			return harnessgen.QueryOperatorInterfaceOutput{Commands: []harnessgen.OperatorCommand{}}, nil // "scope" is not harness-owned
		},
	))
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.SendAgentMessage.Name(),
		func(ctx context.Context, input harnessgen.SendAgentMessageInput, opts nexus.StartOperationOptions) (harnessgen.SendMessageOutput, error) {
			assert.Equal(t, "slash", input.MsgType)
			return harnessgen.SendMessageOutput{TurnNumber: 1}, nil
		},
	))

	env := newTestEnv(t, svc)
	env.ExecuteWorkflow(runStartTurnWorkflow, router.Input{
		SessionID: "slack:C1",
		Slash:     &router.SlashCommand{Name: "scope", Arg: "docs"},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	var result router.StartResult
	require.NoError(t, env.GetWorkflowResult(&result))
	require.NotNil(t, result.Handle)
	assert.Empty(t, result.Reply)
}

func TestStartTurn_Approval_CallsApproveToolCall(t *testing.T) {
	var gotToolID string
	var gotApproved bool
	svc := nexus.NewService(harnessgen.AgentService.ServiceName)
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.ApproveToolCall.Name(),
		func(ctx context.Context, input harnessgen.ApproveToolCallInput, opts nexus.StartOperationOptions) (harnessgen.ApproveToolCallOutput, error) {
			gotToolID, gotApproved = input.ToolId, input.Approved
			return harnessgen.ApproveToolCallOutput{Accepted: true, ToolId: input.ToolId}, nil
		},
	))

	env := newTestEnv(t, svc)
	env.ExecuteWorkflow(runStartTurnWorkflow, router.Input{
		SessionID: "slack:C1",
		Approval:  &router.ApprovalDecision{ToolID: "t1", Approved: true},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	var result router.StartResult
	require.NoError(t, env.GetWorkflowResult(&result))
	assert.Equal(t, router.StartResult{}, result, "approval resolves fire-and-forget")
	assert.Equal(t, "t1", gotToolID)
	assert.True(t, gotApproved)
}

func TestPollTurn_StartsFromCursorAndSkipsStaleEvents(t *testing.T) {
	items := []harnessgen.StreamItem{
		makeTestStreamItem(t, streamItem{TurnID: "old", TurnNumber: 1, Event: turnEvent{Type: "reply_delta", Text: "stale"}}, 0, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnID: "t2", TurnNumber: 2, Event: turnEvent{Type: "reply_delta", Text: "fresh"}}, 1, turnEventsTopic),
	}

	svc := nexus.NewService(harnessgen.AgentService.ServiceName)
	var gotCursor int64
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.PollMessages.Name(),
		func(ctx context.Context, input harnessgen.PollMessagesInput, opts nexus.StartOperationOptions) (harnessgen.PollMessagesOutput, error) {
			gotCursor = input.Cursor
			return harnessgen.PollMessagesOutput{Items: items, NextOffset: 2}, nil
		},
	))

	env := newTestEnv(t, svc)
	env.ExecuteWorkflow(runPollTurnWorkflow, pollTurnWorkflowInput{
		Handle: router.TurnHandle{SessionID: "slack:C1", TurnNumber: 2},
		Cursor: 5,
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	var result router.PollResult
	require.NoError(t, env.GetWorkflowResult(&result))
	assert.Equal(t, int64(5), gotCursor, "PollTurn must start from the given cursor")
	require.Len(t, result.Deltas, 1, "the stale turn-1 event must be dropped")
	assert.Equal(t, "fresh", result.Deltas[0].Text)
	assert.Equal(t, int64(2), result.NextCursor)
	assert.False(t, result.Closed)
}

func TestPollTurn_ToolApprovalRequested_ProducesApprovalDelta(t *testing.T) {
	items := []harnessgen.StreamItem{
		makeTestStreamItem(t, streamItem{TurnID: "t1", TurnNumber: 1, Event: turnEvent{
			Type: "tool_approval_requested", ToolID: "t1", ToolName: "search",
		}}, 0, turnEventsTopic),
	}
	svc := nexus.NewService(harnessgen.AgentService.ServiceName)
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.PollMessages.Name(),
		func(ctx context.Context, input harnessgen.PollMessagesInput, opts nexus.StartOperationOptions) (harnessgen.PollMessagesOutput, error) {
			return harnessgen.PollMessagesOutput{Items: items, NextOffset: 1}, nil
		},
	))

	env := newTestEnv(t, svc)
	env.ExecuteWorkflow(runPollTurnWorkflow, pollTurnWorkflowInput{
		Handle: router.TurnHandle{SessionID: "slack:C1", TurnNumber: 1},
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	var result router.PollResult
	require.NoError(t, env.GetWorkflowResult(&result))
	require.Len(t, result.Deltas, 1)
	require.NotNil(t, result.Deltas[0].ApprovalRequested)
	assert.Equal(t, "search", result.Deltas[0].ApprovalRequested.ToolName)
}

func TestPollTurn_Closed(t *testing.T) {
	svc := nexus.NewService(harnessgen.AgentService.ServiceName)
	svc.MustRegister(nexus.NewSyncOperation(
		harnessgen.AgentService.PollMessages.Name(),
		func(ctx context.Context, input harnessgen.PollMessagesInput, opts nexus.StartOperationOptions) (harnessgen.PollMessagesOutput, error) {
			return harnessgen.PollMessagesOutput{Closed: ptr(true), NextOffset: input.Cursor, Items: []harnessgen.StreamItem{}}, nil
		},
	))

	env := newTestEnv(t, svc)
	env.ExecuteWorkflow(runPollTurnWorkflow, pollTurnWorkflowInput{
		Handle: router.TurnHandle{SessionID: "slack:C1"},
		Cursor: 3,
	})

	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	var result router.PollResult
	require.NoError(t, env.GetWorkflowResult(&result))
	assert.True(t, result.Closed)
	assert.Equal(t, int64(3), result.NextCursor)
}

// -- Test helpers ------------------------------------------------------------

// makeTestStreamItem encodes a streamItem into the wire format expected by decodeTurnEvent:
// base64(proto.Marshal(Payload{encoding:"json/plain", data:<json>}))
func makeTestStreamItem(t *testing.T, si streamItem, offset int64, topic string) harnessgen.StreamItem {
	t.Helper()
	data, err := json.Marshal(si)
	require.NoError(t, err)
	payload := &commonpb.Payload{
		Metadata: map[string][]byte{"encoding": []byte("json/plain")},
		Data:     data,
	}
	b, err := proto.Marshal(payload)
	require.NoError(t, err)
	return harnessgen.StreamItem{
		Topic:  topic,
		Offset: offset,
		Data:   base64.StdEncoding.EncodeToString(b),
	}
}
