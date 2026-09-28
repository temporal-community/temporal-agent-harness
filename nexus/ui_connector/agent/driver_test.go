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

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"
)

func TestTurnEventToEvent_Chunks(t *testing.T) {
	cases := []struct {
		name  string
		event turnEvent
		want  protocol.Chunk
	}{
		{"reply_delta", turnEvent{Type: "reply_delta", Text: "hello"}, protocol.Chunk{Text: "hello"}},
		{"thought_summary", turnEvent{Type: "thought_summary", Delta: map[string]any{"text": "hmm"}},
			protocol.Chunk{Thought: &protocol.ThoughtChunk{Delta: "hmm"}}},
		{"tool_start", turnEvent{Type: "tool_start", ToolID: "t1", ToolName: "search", ToolInput: map[string]any{"q": "x"}},
			protocol.Chunk{Tool: &protocol.ToolChunk{ID: "t1", Name: "search", Phase: protocol.ToolRunning, Input: `{"q":"x"}`}}},
		{"tool_end", turnEvent{Type: "tool_end", ToolID: "t1", ToolName: "search", ToolOutput: "3 hits"},
			protocol.Chunk{Tool: &protocol.ToolChunk{ID: "t1", Name: "search", Phase: protocol.ToolComplete, Output: "3 hits"}}},
		{"tool_error", turnEvent{Type: "tool_error", ToolID: "t1", ToolName: "search", Message: "oops"},
			protocol.Chunk{Tool: &protocol.ToolChunk{ID: "t1", Name: "search", Phase: protocol.ToolErrored, Error: "oops"}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ev, final := turnEventToEvent(tc.event, false)
			require.NotNil(t, ev)
			require.NotNil(t, ev.Chunk)
			assert.Equal(t, tc.want, *ev.Chunk)
			assert.False(t, final)
		})
	}
}

func TestTurnEventToEvent_Ignored(t *testing.T) {
	for _, e := range []turnEvent{
		{Type: "reply_delta"},
		{Type: "thought_summary", Delta: map[string]any{"text": ""}},
		{Type: "text_annotation", Delta: map[string]any{}},
		{Type: "turn_started"},
		{Type: "message_accepted"},
		{Type: "unknown_event"},
	} {
		ev, final := turnEventToEvent(e, false)
		assert.Nil(t, ev, e.Type)
		assert.False(t, final, e.Type)
	}
}

func TestTurnEventToEvent_TextAnnotation_ProducesCitations(t *testing.T) {
	ev, _ := turnEventToEvent(turnEvent{
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
				map[string]any{"type": "file_citation", "file_name": "notes.txt"},
			},
		},
	}, false)
	require.NotNil(t, ev)
	require.Len(t, ev.Chunk.Citations, 2)
	assert.Equal(t, protocol.Citation{URL: "https://example.com/runbook#section-2", Title: "Section 2", EndIndex: 42}, ev.Chunk.Citations[0])
	assert.Equal(t, protocol.Citation{URL: "", Title: "notes.txt", EndIndex: -1}, ev.Chunk.Citations[1])
}

func TestTurnEventToEvent_Approvals(t *testing.T) {
	req, _ := turnEventToEvent(turnEvent{
		Type: "tool_approval_requested", ToolID: "t1", ToolName: "search",
		ToolInput: map[string]any{"query": "foo"},
	}, false)
	require.NotNil(t, req.ApprovalRequested)
	assert.JSONEq(t, `{"query":"foo"}`, req.ApprovalRequested.ToolInputJSON)

	res, _ := turnEventToEvent(turnEvent{
		Type: "tool_approval_resolved", ToolID: "t1", ToolName: "search",
		Approved: true, Remember: true, Reason: "by U",
	}, false)
	assert.Equal(t, &protocol.ApprovalResolution{ToolID: "t1", ToolName: "search", Approved: true, Remember: true, Reason: "by U"}, res.ApprovalResolved)
}

// A turn-scoped reply ends at turn_end. A message-scoped reply ends at its own
// handler's end, because turn_end may belong to a turn the message only joined.
func TestTurnEventToEvent_TerminalEvents(t *testing.T) {
	ev, final := turnEventToEvent(turnEvent{Type: "turn_end"}, false)
	assert.Nil(t, ev)
	assert.True(t, final)
	_, final = turnEventToEvent(turnEvent{Type: "turn_end"}, true)
	assert.False(t, final)

	for _, scoped := range []bool{false, true} {
		out, final := turnEventToEvent(turnEvent{Type: "message_handler_end", Output: map[string]any{"text": "Done!"}}, scoped)
		assert.Equal(t, map[string]any{"text": "Done!"}, out.HandlerOutput.Value)
		assert.Equal(t, scoped, final)

		failed, final := turnEventToEvent(turnEvent{Type: "message_handler_error", Message: "boom"}, scoped)
		assert.Equal(t, "The agent's handler failed: boom", failed.Error)
		assert.Equal(t, scoped, final)
	}
}

func TestDecodeTurnEvent_RoundTrip(t *testing.T) {
	si := streamItem{TurnID: "t1", TurnNumber: 3, MessageID: "msg-1", Timestamp: 1700000000.0,
		Event: turnEvent{Type: "reply_delta", Text: "hello"}}
	got, err := decodeTurnEvent(makeTestStreamItem(t, si, 0, turnEventsTopic))
	require.NoError(t, err)
	assert.Equal(t, 3, got.TurnNumber)
	assert.Equal(t, "msg-1", got.MessageID)
	assert.Equal(t, "hello", got.Event.Text)
}

func runStart(ctx workflow.Context, req protocol.Request) (router.Start, error) {
	return (&Driver{}).Start(ctx, req)
}

func runPoll(ctx workflow.Context, handle router.Handle) (router.Poll, error) {
	return (&Driver{}).Poll(ctx, handle)
}

func newTestEnv(t *testing.T, svc *nexus.Service) *testsuite.TestWorkflowEnvironment {
	t.Helper()
	env := (&testsuite.WorkflowTestSuite{}).NewTestWorkflowEnvironment()
	env.RegisterWorkflow(runStart)
	env.RegisterWorkflow(runPoll)
	env.RegisterNexusService(svc)
	return env
}

func agentService(ops ...nexus.RegisterableOperation) *nexus.Service {
	svc := nexus.NewService(harnessgen.AgentService.ServiceName)
	svc.MustRegister(ops...)
	return svc
}

func sendOp(got *harnessgen.SendAgentMessageInput) nexus.RegisterableOperation {
	return nexus.NewSyncOperation(harnessgen.AgentService.SendAgentMessage.Name(),
		func(ctx context.Context, input harnessgen.SendAgentMessageInput, _ nexus.StartOperationOptions) (harnessgen.SendMessageOutput, error) {
			*got = input
			return harnessgen.SendMessageOutput{TurnNumber: 2, StreamHeadOffset: ptr(int64(5)), MessageId: ptr("msg-1")}, nil
		})
}

const setModelSchema = `{"properties":{"model":{"enum":["fast","smart"],"type":"string"}},"required":["model"],"type":"object"}`

func interfaceOp(fail bool) nexus.RegisterableOperation {
	return nexus.NewSyncOperation(harnessgen.AgentService.QueryAgentInterface.Name(),
		func(ctx context.Context, input harnessgen.QuerySessionInput, _ nexus.StartOperationOptions) (harnessgen.AgentInterfaceOutput, error) {
			if fail {
				return harnessgen.AgentInterfaceOutput{}, nexus.HandlerErrorf(nexus.HandlerErrorTypeNotFound, "no session")
			}
			accept := harnessgen.AcceptedFunctionMidTurnAccept
			return harnessgen.AgentInterfaceOutput{Handlers: []harnessgen.AcceptedFunction{
				{Name: "ask", Description: "Chat.", Parameters: `{"properties":{"text":{"type":"string"}},"required":["text"]}`, Output: "{}"},
				{Name: "set_model", Description: "Set the model.", Parameters: setModelSchema, Output: "{}", MidTurn: &accept},
			}}, nil
		})
}

func start(t *testing.T, svc *nexus.Service, req protocol.Request) router.Start {
	t.Helper()
	env := newTestEnv(t, svc)
	env.ExecuteWorkflow(runStart, req)
	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	var result router.Start
	require.NoError(t, env.GetWorkflowResult(&result))
	return result
}

func message(text string) protocol.Request {
	return protocol.Request{SessionID: "slack:C1:1.0", Message: &protocol.Message{Text: text}}
}

func replyText(t *testing.T, r router.Start) string {
	t.Helper()
	require.Len(t, r.Events, 1)
	require.NotNil(t, r.Events[0].Chunk)
	return r.Events[0].Chunk.Text
}

func TestStart_PlainTextGoesToAsk(t *testing.T) {
	var got harnessgen.SendAgentMessageInput
	result := start(t, agentService(sendOp(&got)), message(" hi there "))

	assert.Equal(t, "ask", got.MsgType)
	assert.JSONEq(t, `{"text":"hi there"}`, got.Payload)
	assert.Equal(t, []protocol.Event{{TurnStarted: true}}, result.Events)
	assert.JSONEq(t, `{"sessionId":"slack:C1:1.0","turnNumber":2,"messageId":"msg-1","cursor":5}`, string(result.Handle))
}

func TestStart_FirstContactAddsTheGuide(t *testing.T) {
	var got harnessgen.SendAgentMessageInput
	req := message("hi")
	req.Message.FirstContact = true
	result := start(t, agentService(sendOp(&got), interfaceOp(false)), req)

	require.Len(t, result.Events, 2)
	guide := result.Events[0].Guide
	require.NotNil(t, guide)
	require.Len(t, guide.Handlers, 2)
	h := guide.Handlers[1]
	assert.Equal(t, "set_model", h.Name)
	assert.Equal(t, "accept", h.MidTurn)
	assert.Equal(t, []string{"fast", "smart"}, h.Fields[0].Options)
	assert.Equal(t, []string{"fast", "smart"}, h.Fields[0].Options)
	assert.True(t, result.Events[1].TurnStarted)
}

func TestStart_FirstContactGuideFailureKeepsTheReply(t *testing.T) {
	var got harnessgen.SendAgentMessageInput
	req := message("hi")
	req.Message.FirstContact = true
	result := start(t, agentService(sendOp(&got), interfaceOp(true)), req)
	assert.Equal(t, []protocol.Event{{TurnStarted: true}}, result.Events)
	assert.NotNil(t, result.Handle)
}

func TestStart_Help(t *testing.T) {
	result := start(t, agentService(interfaceOp(false)), message("/help"))
	require.Len(t, result.Events, 1)
	assert.Len(t, result.Events[0].Guide.Handlers, 2)
	assert.True(t, router.Empty(result.Handle))

	result = start(t, agentService(interfaceOp(true)), message("/help"))
	assert.Equal(t, noSessionReply, replyText(t, result))
}

func TestStart_HandlerFromText(t *testing.T) {
	var got harnessgen.SendAgentMessageInput
	result := start(t, agentService(sendOp(&got), interfaceOp(false)), message("/set_model model=smart"))
	assert.Equal(t, "set_model", got.MsgType)
	assert.JSONEq(t, `{"model":"smart"}`, got.Payload)
	assert.NotNil(t, result.Handle)

	bad := start(t, agentService(interfaceOp(false)), message("/set_model model=huge"))
	require.Len(t, bad.Events, 1)
	help := bad.Events[0].HandlerHelp
	require.NotNil(t, help)
	assert.Equal(t, "set_model", help.Handler.Name)
	assert.Equal(t, map[string]string{"model": "Must be one of: fast, smart."}, help.Errors)

	missing := start(t, agentService(interfaceOp(false)), message("/set_model"))
	require.NotNil(t, missing.Events[0].HandlerHelp)
	assert.Empty(t, missing.Events[0].HandlerHelp.Errors)
	assert.Equal(t, "Unknown handler \"nope\". This agent accepts: ask, set_model",
		replyText(t, start(t, agentService(interfaceOp(false)), message("/nope"))))
}

func TestStart_CallSendsPayloadVerbatim(t *testing.T) {
	var got harnessgen.SendAgentMessageInput
	result := start(t, agentService(sendOp(&got)), protocol.Request{
		SessionID: "slack:C1:1.0",
		Call:      &protocol.HandlerCall{Name: "set_model", Payload: `{"model":"fast"}`},
	})
	assert.Equal(t, "set_model", got.MsgType)
	assert.Equal(t, `{"model":"fast"}`, got.Payload)
	assert.NotNil(t, result.Handle)
}

func TestStart_OpenFormReturnsTheHandler(t *testing.T) {
	result := start(t, agentService(interfaceOp(false)), protocol.Request{
		SessionID: "slack:C1:1.0", OpenForm: &protocol.FormRequest{Handler: "set_model"},
	})
	require.Len(t, result.Events, 1)
	assert.Equal(t, "set_model", result.Events[0].Form.Name)
}

func TestStart_ApprovalCallsApproveToolCallAndReturnsTheDecision(t *testing.T) {
	var got harnessgen.ApproveToolCallInput
	svc := agentService(nexus.NewSyncOperation(harnessgen.AgentService.ApproveToolCall.Name(),
		func(ctx context.Context, input harnessgen.ApproveToolCallInput, _ nexus.StartOperationOptions) (harnessgen.ApproveToolCallOutput, error) {
			got = input
			return harnessgen.ApproveToolCallOutput{Accepted: true, ToolId: input.ToolId}, nil
		}))
	result := start(t, svc, protocol.Request{
		SessionID: "slack:C1",
		Approval:  &protocol.ApprovalDecision{ToolID: "t1", ToolName: "search", Approved: true, Remember: true, Reason: "by U"},
	})

	assert.Equal(t, "t1", got.ToolId)
	assert.Equal(t, ptr(true), got.Remember)
	assert.Equal(t, ptr("by U"), got.Reason)
	assert.True(t, router.Empty(result.Handle))
	assert.Equal(t, []protocol.Event{{ApprovalResolved: &protocol.ApprovalResolution{
		ToolID: "t1", ToolName: "search", Approved: true, Remember: true, Reason: "by U",
	}}}, result.Events)
}

func poll(t *testing.T, items []harnessgen.StreamItem, closed bool, h turnHandle) router.Poll {
	t.Helper()
	var gotCursor int64
	svc := agentService(nexus.NewSyncOperation(harnessgen.AgentService.PollMessages.Name(),
		func(ctx context.Context, input harnessgen.PollMessagesInput, _ nexus.StartOperationOptions) (harnessgen.PollMessagesOutput, error) {
			gotCursor = input.Cursor
			return harnessgen.PollMessagesOutput{Items: items, NextOffset: input.Cursor + int64(len(items)), Closed: ptr(closed)}, nil
		}))
	handle, err := json.Marshal(h)
	require.NoError(t, err)
	env := newTestEnv(t, svc)
	env.ExecuteWorkflow(runPoll, router.Handle(handle))
	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	var result router.Poll
	require.NoError(t, env.GetWorkflowResult(&result))
	assert.Equal(t, h.Cursor, gotCursor, "Poll reads from the handle's cursor")
	return result
}

func TestPoll_TurnScopedSkipsOtherTurnsAndAdvancesTheCursor(t *testing.T) {
	items := []harnessgen.StreamItem{
		makeTestStreamItem(t, streamItem{TurnNumber: 1, Event: turnEvent{Type: "reply_delta", Text: "stale"}}, 0, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnNumber: 2, Event: turnEvent{Type: "reply_delta", Text: "fresh"}}, 1, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnNumber: 2, Event: turnEvent{Type: "reply_delta", Text: "other topic"}}, 2, "other"),
	}
	result := poll(t, items, false, turnHandle{SessionID: "s", TurnNumber: 2, Cursor: 7})
	require.Len(t, result.Events, 1)
	assert.Equal(t, "fresh", result.Events[0].Chunk.Text)
	assert.False(t, result.Done)
	assert.JSONEq(t, `{"sessionId":"s","turnNumber":2,"cursor":10}`, string(result.Handle))
}

// A message that joined an open turn shows only its own events, and ends at its own
// handler's end.
func TestPoll_MessageScopedSkipsOtherMessages(t *testing.T) {
	items := []harnessgen.StreamItem{
		makeTestStreamItem(t, streamItem{TurnNumber: 1, MessageID: "other", Event: turnEvent{Type: "reply_delta", Text: "not mine"}}, 0, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnNumber: 1, MessageID: "mine", Event: turnEvent{Type: "message_handler_end", Output: map[string]any{"text": "Set."}}}, 1, turnEventsTopic),
		makeTestStreamItem(t, streamItem{TurnNumber: 1, MessageID: "mine", Event: turnEvent{Type: "reply_delta", Text: "after the end"}}, 2, turnEventsTopic),
	}
	result := poll(t, items, false, turnHandle{SessionID: "s", TurnNumber: 1, MessageID: "mine"})
	require.Len(t, result.Events, 1, "events after the end are dropped")
	assert.Equal(t, map[string]any{"text": "Set."}, result.Events[0].HandlerOutput.Value)
	assert.True(t, result.Done)
}

func TestPoll_Closed(t *testing.T) {
	result := poll(t, []harnessgen.StreamItem{}, true, turnHandle{SessionID: "s", Cursor: 3})
	assert.True(t, result.Done)
	assert.Empty(t, result.Events)
}

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

func TestStartPreservesLiteralMentions(t *testing.T) {
	var got harnessgen.SendAgentMessageInput
	start(t, agentService(sendOp(&got)), message("<@U1> is the on-call engineer"))
	assert.JSONEq(t, `{"text":"<@U1> is the on-call engineer"}`, got.Payload)
}
