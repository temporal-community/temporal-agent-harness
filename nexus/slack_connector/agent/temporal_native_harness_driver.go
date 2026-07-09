package agent

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"time"

	agentgen "github.com/temporal-community/temporal-agent-harness/nexus/slack_connector/agent/generated"
	msgiface "github.com/temporal-community/temporal-agent-harness/nexus/slack_connector/messaging"
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
		return &agentDelta{Text: " ✅\n"}
	case "tool_error":
		return &agentDelta{Text: " ❌ Error: " + e.Message + "_\n"}
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
		TurnNumber:       sendOut.TurnNumber,
		StreamHeadOffset: sendOut.StreamHeadOffset,
	}, nil
}

// RespondToPlatform polls the Nexus agent response stream starting from the cursor in
// handle and delivers each delta back to the platform via the registered Stream activity.
func (d *TemporalNativeHarnessDriver) RespondToPlatform(ctx workflow.Context, handle TurnHandle, input ConnectorWorkflowInput) error {
	agentClient := workflow.NewNexusClient(AgentNexusEndpoint, agentgen.AgentService.ServiceName)
	actCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: 30 * time.Second,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 1},
	})

	cursor := handle.StreamHeadOffset
	var streamID string

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
			if streamID != "" {
				_ = workflow.ExecuteActivity(actCtx, msgiface.StreamActivity, msgiface.StreamInput{
					TextMetadata: msgiface.TextMetadata{SessionID: input.SessionID},
					StreamID:     streamID,
					DeltaType:    msgiface.DeltaTypeEnd,
				}).Get(ctx, nil)
			}
			return nil
		}

		cursor = pollOut.NextOffset

		if pollOut.Closed {
			if streamID != "" {
				_ = workflow.ExecuteActivity(actCtx, msgiface.StreamActivity, msgiface.StreamInput{
					TextMetadata: msgiface.TextMetadata{SessionID: input.SessionID},
					StreamID:     streamID,
					DeltaType:    msgiface.DeltaTypeEnd,
				}).Get(ctx, nil)
			}
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
				inputJSON, _ := json.Marshal(event.ToolInput)
				_ = workflow.ExecuteActivity(actCtx, msgiface.PostApprovalPromptActivity,
					msgiface.ApprovalPromptInput{
						SessionID: input.SessionID,
						ThreadID:  input.ThreadID(),
						ToolID:    event.ToolID,
						ToolName:  event.ToolName,
						ToolInput: string(inputJSON),
					}).Get(ctx, nil)
				continue
			}

			delta := turnEventToDelta(*event)
			if delta == nil {
				continue
			}

			if delta.Text != "" {
				// streamID == "" iff this is the first delta, so we start the platform
				// stream to get the ID for subsequent appends.
				if streamID == "" {
					var sid string
					if err := workflow.ExecuteActivity(actCtx, msgiface.StreamActivity, msgiface.StreamInput{
						TextMetadata: msgiface.TextMetadata{
							SessionID: input.SessionID,
							ThreadID:  input.ThreadID(),
							SenderID:  input.SenderID(),
						},
						DeltaType: msgiface.DeltaTypeStart,
					}).Get(ctx, &sid); err != nil {
						workflow.GetLogger(ctx).Warn("RespondToPlatform: stream start failed, falling back to postMessage", "error", err)
						_ = workflow.ExecuteActivity(actCtx, msgiface.PostMessageActivity, msgiface.TextMetadata{
							SessionID: input.SessionID,
							ThreadID:  input.ThreadID(),
							Text:      delta.Text,
						}).Get(ctx, nil)
						return nil
					}
					streamID = sid
				}

				if err := workflow.ExecuteActivity(actCtx, msgiface.StreamActivity, msgiface.StreamInput{
					TextMetadata: msgiface.TextMetadata{
						SessionID: input.SessionID,
						Text:      delta.Text,
					},
					StreamID:  streamID,
					DeltaType: msgiface.DeltaTypeAppend,
				}).Get(ctx, nil); err != nil {
					workflow.GetLogger(ctx).Warn("RespondToPlatform: stream append failed", "error", err)
				}
			}

			if delta.IsFinal {
				if streamID != "" {
					_ = workflow.ExecuteActivity(actCtx, msgiface.StreamActivity, msgiface.StreamInput{
						TextMetadata: msgiface.TextMetadata{SessionID: input.SessionID},
						StreamID:     streamID,
						DeltaType:    msgiface.DeltaTypeEnd,
					}).Get(ctx, nil)
				}
				return nil
			}
		}
	}
}
