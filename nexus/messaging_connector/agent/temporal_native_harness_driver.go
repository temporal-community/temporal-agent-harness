package agent

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	agentgen "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/agent/generated"
	msgiface "github.com/temporal-community/temporal-agent-harness/nexus/messaging_connector/messaging"
	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"
)

const (
	// AgentNexusEndpoint is the Nexus endpoint name the driver targets.
	AgentNexusEndpoint = "nexus-agent-endpoint"
	turnEventsTopic    = "turn_events"
)

// turnEvent matches harness/agent_client.py TurnEvent (json/plain, snake_case).
// streamItem is the outer wrapper from WorkflowStream._log.
type streamItem struct {
	TurnID     string    `json:"turn_id"`
	TurnNumber int       `json:"turn_number"`
	Timestamp  float64   `json:"timestamp"`
	Event      turnEvent `json:"event"`
}

type turnEvent struct {
	Type       string         `json:"type"`
	Text       string         `json:"text"`
	ToolID     string         `json:"tool_id"`
	ToolName   string         `json:"tool_name"`
	ToolInput  map[string]any `json:"tool_input"`
	ToolOutput string         `json:"tool_output"`
	Message    string         `json:"message"`
	Delta      map[string]any `json:"delta"`
}

type agentDelta struct {
	Text    string
	IsFinal bool
}

func decodeTurnEvent(item agentgen.ItemElement) (int, *turnEvent, error) {
	b, err := base64.StdEncoding.DecodeString(item.Data)
	if err != nil {
		b, err = base64.URLEncoding.DecodeString(item.Data)
		if err != nil {
			return 0, nil, fmt.Errorf("base64: %w", err)
		}
	}
	var payload commonpb.Payload
	if err := proto.Unmarshal(b, &payload); err != nil {
		return 0, nil, fmt.Errorf("unmarshal Payload: %w", err)
	}
	var si streamItem
	if err := json.Unmarshal(payload.Data, &si); err != nil {
		return 0, nil, fmt.Errorf("unmarshal streamItem: %w", err)
	}
	return si.TurnNumber, &si.Event, nil
}

func turnEventToDelta(e turnEvent) *agentDelta {
	switch e.Type {
	case "reply_delta":
		return &agentDelta{Text: e.Text}
	case "thought_summary":
		if text, ok := e.Delta["text"].(string); ok && text != "" {
			return &agentDelta{Text: text}
		}
	case "tool_start":
		return &agentDelta{Text: "\n_" + e.ToolName + "..._"}
	case "tool_end":
		return &agentDelta{Text: " ✅\n\n"}
	case "tool_error":
		return &agentDelta{Text: "\n❌ Error: " + e.Message + "\n\n"}
	case "reply":
		// Text was already fully streamed via reply_delta events; this just signals completion.
		return &agentDelta{IsFinal: true}
	case "error":
		return &agentDelta{Text: "[error] " + e.Message, IsFinal: true}
	}
	return nil
}

// TemporalNativeHarnessDriver implements MessageHandler using the Nexus agent service.
type TemporalNativeHarnessDriver struct{}

// deliveryState is workflow-owned state for one platform response segment.
// Temporal rebuilds it through replay, so stream activities can run on any
// worker without relying on process-local maps.
type deliveryState struct {
	Handle       *msgiface.StreamHandle
	FullText     string
	PendingDelta string
	LastFlush    time.Time
	Segment      int
}

// ReceiveMessageFromPlatform dispatches the incoming message to the Nexus agent and
// returns a TurnHandle that RespondToPlatform uses to consume the response stream.
func (d *TemporalNativeHarnessDriver) ReceiveMessageFromPlatform(ctx workflow.Context, input ConnectorWorkflowInput) (TurnHandle, error) {
	agentClient := workflow.NewNexusClient(AgentNexusEndpoint, agentgen.AgentService.ServiceName)
	// Encode the "ask" payload as JSON — sendAgentMessage is generic over any @agent.accepts handler.
	// Message is non-nil here: ReceiveMessageFromPlatform is only called for Message interactions.
	payload := fmt.Sprintf(`{"text":%q}`, input.Message.Text)
	var sendOut agentgen.SendMessageOutput
	if err := agentClient.ExecuteOperation(ctx, agentgen.AgentService.SendAgentMessage,
		agentgen.SendAgentMessageInput{
			SessionID: input.SessionID,
			MsgType:   "ask",
			Payload:   payload,
		},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 60 * time.Second},
	).Get(ctx, &sendOut); err != nil {
		return TurnHandle{}, err
	}
	return TurnHandle{
		TurnID:           sendOut.TurnID,
		TurnNumber:       sendOut.TurnNumber,
		StreamHeadOffset: sendOut.StreamHeadOffset,
	}, nil
}

// RespondToPlatform polls the Nexus agent response stream starting from the cursor in
// handle and delivers each delta through stateless platform activities.
func (d *TemporalNativeHarnessDriver) RespondToPlatform(ctx workflow.Context, handle TurnHandle, input ConnectorWorkflowInput) error {
	agentClient := workflow.NewNexusClient(AgentNexusEndpoint, agentgen.AgentService.ServiceName)
	isTeams := strings.HasPrefix(input.SessionID, "teams:")
	activityTimeout := 30 * time.Second
	if isTeams {
		// Teams activities may wait inside an attempt to honor Retry-After.
		activityTimeout = 5 * time.Minute
	}
	actCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: activityTimeout,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
	})

	cursor := handle.StreamHeadOffset
	conversationType := ""
	if input.Message != nil {
		conversationType = input.Message.ConversationType
	}
	turnID := handle.TurnID
	if turnID == "" {
		turnID = fmt.Sprintf("turn-%d", handle.TurnNumber)
	}
	workflowID := workflow.GetInfo(ctx).WorkflowExecution.ID
	state := deliveryState{}
	textMetadata := func(text string) msgiface.TextMetadata {
		return msgiface.TextMetadata{
			SessionID:  input.SessionID,
			ThreadID:   input.ThreadID(),
			SenderID:   input.SenderID(),
			Text:       text,
			ServiceURL: input.ServiceURL(),
			ChannelID:  input.ChannelID(),
		}
	}

	operationID := func(phase string, sequence int) string {
		return fmt.Sprintf("%s/%s/segment/%d/%s/%d", workflowID, turnID, state.Segment, phase, sequence)
	}

	beginStream := func() error {
		if state.Handle != nil {
			return nil
		}
		var streamHandle msgiface.StreamHandle
		if err := workflow.ExecuteActivity(actCtx, msgiface.BeginStreamActivity, msgiface.BeginStreamInput{
			TextMetadata:     textMetadata(""),
			ConversationType: conversationType,
			OperationID:      operationID("begin", 0),
		}).Get(ctx, &streamHandle); err != nil {
			return err
		}
		state.Handle = &streamHandle
		state.LastFlush = workflow.Now(ctx)
		return nil
	}

	flushUpdate := func(force bool) error {
		if state.Handle == nil || state.PendingDelta == "" {
			return nil
		}
		now := workflow.Now(ctx)
		if !force && state.Handle.MinUpdateInterval > 0 && now.Sub(state.LastFlush) < state.Handle.MinUpdateInterval {
			return nil
		}
		sequence := state.Handle.NextSequence
		if err := workflow.ExecuteActivity(actCtx, msgiface.UpdateStreamActivity, msgiface.UpdateStreamInput{
			TextMetadata: textMetadata(""),
			Handle:       *state.Handle,
			Delta:        state.PendingDelta,
			FullText:     state.FullText,
			Sequence:     sequence,
			OperationID:  operationID("update", sequence),
		}).Get(ctx, nil); err != nil {
			workflow.GetLogger(ctx).Warn("RespondToPlatform: stream update failed", "error", err)
			return err
		}
		state.PendingDelta = ""
		if state.Handle.NextSequence > 0 {
			state.Handle.NextSequence++
		}
		state.LastFlush = workflow.Now(ctx)
		return nil
	}

	finishStream := func() {
		if state.Handle == nil {
			return
		}
		// Full-text transports are caught up by FinishStream itself. Delta
		// transports need one final append before the provider stream is stopped.
		if state.Handle.WireTextMode == msgiface.StreamWireTextDelta {
			_ = flushUpdate(true)
		}
		if err := workflow.ExecuteActivity(actCtx, msgiface.FinishStreamActivity, msgiface.FinishStreamInput{
			TextMetadata: textMetadata(""),
			Handle:       *state.Handle,
			FullText:     state.FullText,
			OperationID:  operationID("finish", state.Handle.NextSequence),
		}).Get(ctx, nil); err != nil {
			workflow.GetLogger(ctx).Warn("RespondToPlatform: stream finish failed", "error", err)
		}
	}

	resetForNextSegment := func() {
		nextSegment := state.Segment + 1
		state = deliveryState{Segment: nextSegment}
	}

	for {
		var pollOut agentgen.PollMessagesOutput
		if err := agentClient.ExecuteOperation(ctx, agentgen.AgentService.PollMessages,
			agentgen.PollMessagesInput{
				SessionID:      input.SessionID,
				Cursor:         cursor,
				TimeoutSeconds: 5,
			},
			workflow.NexusOperationOptions{ScheduleToCloseTimeout: 120 * time.Second},
		).Get(ctx, &pollOut); err != nil {
			workflow.GetLogger(ctx).Warn("RespondToPlatform: pollMessages failed", "error", err)
			finishStream()
			return nil
		}

		cursor = pollOut.NextOffset

		if pollOut.Closed {
			finishStream()
			return nil
		}

		for _, item := range pollOut.Items {
			if item.Topic != turnEventsTopic {
				continue
			}
			turnNumber, event, err := decodeTurnEvent(item)
			if err != nil {
				workflow.GetLogger(ctx).Warn("RespondToPlatform: decodeTurnEvent failed", "error", err)
				continue
			}
			if turnNumber < int(handle.TurnNumber) {
				continue
			}

			// Tool approval gates — post interactive prompt and let the approval
			// workflow (started by the interaction webhook) call approveToolCall.
			if event.Type == "tool_approval_requested" {
				if state.Handle != nil && state.Handle.CloseBeforeApproval {
					finishStream()
					// Always force post-approval output into a new platform message, even
					// if finalising the prior stream failed.
					resetForNextSegment()
				}
				inputJSON, _ := json.Marshal(event.ToolInput)
				_ = workflow.ExecuteActivity(actCtx, msgiface.PostApprovalPromptActivity,
					msgiface.ApprovalPromptInput{
						SessionID:  input.SessionID,
						ThreadID:   input.ThreadID(),
						ServiceURL: input.ServiceURL(),
						ChannelID:  input.ChannelID(),
						ToolID:     event.ToolID,
						ToolName:   event.ToolName,
						ToolInput:  string(inputJSON),
					}).Get(ctx, nil)
				continue
			}

			delta := turnEventToDelta(*event)
			if delta == nil {
				continue
			}

			if delta.Text != "" {
				state.FullText += delta.Text
				state.PendingDelta += delta.Text
				if err := beginStream(); err != nil {
					workflow.GetLogger(ctx).Warn("RespondToPlatform: stream begin failed, falling back to postMessage", "error", err)
					_ = workflow.ExecuteActivity(actCtx, msgiface.PostMessageActivity, textMetadata(state.FullText)).Get(ctx, nil)
					return nil
				}
				_ = flushUpdate(false)
			}

			if delta.IsFinal {
				finishStream()
				return nil
			}
		}
	}
}
