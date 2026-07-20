// Package temporal_agent_harness implements outbound.Driver against the
// temporal-agent-harness's Nexus agent service. This enables us to know
// how to route a message from the inbound driver to the temporal-agent-harness.
package temporal_agent_harness

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"time"

	harnessgen "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound/driver/temporal_agent_harness/generated"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/wire"
	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"
)

const (
	// AgentNexusEndpoint is the Nexus endpoint name the driver targets.
	AgentNexusEndpoint = "support-agent-nexus"
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

func decodeTurnEvent(item harnessgen.ItemElement) (int, *turnEvent, error) {
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

// turnEventToDelta maps one decoded turn event to a generic outbound.Delta, or nil if
// the event type carries no deliverable content.
func turnEventToDelta(e turnEvent) *outbound.Delta {
	switch e.Type {
	case "reply_delta":
		return &outbound.Delta{Text: e.Text}
	case "thought_summary":
		if text, ok := e.Delta["text"].(string); ok && text != "" {
			return &outbound.Delta{Text: text}
		}
	case "tool_start":
		return &outbound.Delta{Text: "\n_" + e.ToolName + "..._"}
	case "tool_end":
		return &outbound.Delta{Text: " ✅\n"}
	case "tool_error":
		return &outbound.Delta{Text: " ❌ Error: " + e.Message + "_\n"}
	case "reply":
		// Text was already fully streamed via reply_delta events; this just signals completion.
		return &outbound.Delta{IsFinal: true}
	case "error":
		return &outbound.Delta{Text: "[error] " + e.Message, IsFinal: true}
	case "tool_approval_requested":
		// Tool approval gates surface as a delta with no text; the approval workflow
		// (started by the interaction webhook) later calls approveToolCall, which
		// StartTurn's Approval case resolves.
		inputJSON, _ := json.Marshal(e.ToolInput)
		return &outbound.Delta{ApprovalRequested: &outbound.ApprovalRequest{
			ToolID:        e.ToolID,
			ToolName:      e.ToolName,
			ToolInputJSON: string(inputJSON),
		}}
	}
	return nil
}

// Driver implements outbound.Driver against the temporal-agent-harness's Nexus agent
// service.
type Driver struct{}

// StartTurn dispatches a message, slash command, or approval decision to the agent
// nexus service, translating the backend's response into a generic outbound.StartResult.
func (d *Driver) StartTurn(ctx workflow.Context, input wire.Input) (outbound.StartResult, error) {
	agentClient := workflow.NewNexusClient(AgentNexusEndpoint, harnessgen.AgentService.ServiceName)

	switch {
	case input.Message != nil:
		payload := fmt.Sprintf(`{"text":%q}`, input.Message.Text)
		sendOut, err := sendAgentMessage(ctx, agentClient, input.SessionID, "ask", payload)
		if err != nil {
			return outbound.StartResult{}, err
		}
		return outbound.StartResult{Handle: &outbound.TurnHandle{
			SessionID:        input.SessionID,
			TurnNumber:       sendOut.TurnNumber,
			StreamHeadOffset: sendOut.StreamHeadOffset,
		}}, nil

	case input.Slash != nil:
		return startSlashTurn(ctx, agentClient, input.SessionID, input.Slash)

	case input.Approval != nil:
		return resolveApproval(ctx, agentClient, input.SessionID, input.Approval)

	default:
		return outbound.StartResult{}, nil
	}
}

func sendAgentMessage(ctx workflow.Context, agentClient workflow.NexusClient, sessionID, msgType, payload string) (harnessgen.SendMessageOutput, error) {
	var sendOut harnessgen.SendMessageOutput
	err := agentClient.ExecuteOperation(ctx, harnessgen.AgentService.SendAgentMessage,
		harnessgen.SendAgentMessageInput{SessionID: sessionID, MsgType: msgType, Payload: payload},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 60 * time.Second},
	).Get(ctx, &sendOut)
	return sendOut, err
}

// startSlashTurn decides whether s names a harness-owned operator command (synchronous,
// no turn) or an agent-owned slash command (creates a turn), and dispatches accordingly.
// Agent-owned slash commands (@agent.accepts slash) are NOT in the operator interface -
// they route directly to sendAgentMessage(type="slash").
func startSlashTurn(ctx workflow.Context, agentClient workflow.NexusClient, sessionID string, s *wire.SlashCommand) (outbound.StartResult, error) {
	var ifaceOut harnessgen.QueryOperatorInterfaceOutput
	if err := agentClient.ExecuteOperation(ctx, harnessgen.AgentService.QueryOperatorInterface,
		harnessgen.QuerySessionInput{SessionID: sessionID},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 10 * time.Second},
	).Get(ctx, &ifaceOut); err != nil {
		return outbound.StartResult{Reply: "_No active session. Start a conversation first before using slash commands._"}, nil
	}

	var cmd *harnessgen.CommandElement
	for i := range ifaceOut.Commands {
		if ifaceOut.Commands[i].Name == s.Name {
			cmd = &ifaceOut.Commands[i]
			break
		}
	}

	if cmd != nil && cmd.Source == "harness" {
		// Harness operator command: synchronous, no turn, returns text directly.
		var opOut harnessgen.ExecuteOperatorCommandOutput
		if err := agentClient.ExecuteOperation(ctx, harnessgen.AgentService.ExecuteOperatorCommand,
			harnessgen.ExecuteOperatorCommandInput{SessionID: sessionID, Name: s.Name, Arg: s.Arg},
			workflow.NexusOperationOptions{ScheduleToCloseTimeout: 30 * time.Second},
		).Get(ctx, &opOut); err != nil {
			return outbound.StartResult{Reply: fmt.Sprintf("_Command failed: %v_", err)}, nil
		}
		return outbound.StartResult{Reply: opOut.Reply}, nil
	}

	// Agent slash command: creates a turn, same as a message.
	payload, _ := json.Marshal(map[string]string{"name": s.Name, "arg": s.Arg})
	sendOut, err := sendAgentMessage(ctx, agentClient, sessionID, "slash", string(payload))
	if err != nil {
		return outbound.StartResult{Reply: fmt.Sprintf("_Command failed: %v_", err)}, nil
	}
	return outbound.StartResult{Handle: &outbound.TurnHandle{
		SessionID:        sessionID,
		TurnNumber:       sendOut.TurnNumber,
		StreamHeadOffset: sendOut.StreamHeadOffset,
	}}, nil
}

func resolveApproval(ctx workflow.Context, agentClient workflow.NexusClient, sessionID string, a *wire.ApprovalDecision) (outbound.StartResult, error) {
	var out harnessgen.ApproveToolCallOutput
	if err := agentClient.ExecuteOperation(ctx, harnessgen.AgentService.ApproveToolCall,
		harnessgen.ApproveToolCallInput{SessionID: sessionID, ToolID: a.ToolID, Approved: a.Approved},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 30 * time.Second},
	).Get(ctx, &out); err != nil {
		workflow.GetLogger(ctx).Warn("StartTurn: approveToolCall failed",
			"toolId", a.ToolID, "approved", a.Approved, "error", err)
	}
	return outbound.StartResult{}, nil
}

// PollTurn polls the Nexus agent response stream starting from cursor and decodes each
// item into a generic outbound.Delta.
func (d *Driver) PollTurn(ctx workflow.Context, handle outbound.TurnHandle, cursor int64) (outbound.PollResult, error) {
	agentClient := workflow.NewNexusClient(AgentNexusEndpoint, harnessgen.AgentService.ServiceName)

	var pollOut harnessgen.PollMessagesOutput
	if err := agentClient.ExecuteOperation(ctx, harnessgen.AgentService.PollMessages,
		harnessgen.PollMessagesInput{
			SessionID:      handle.SessionID,
			Cursor:         cursor,
			TimeoutSeconds: 5,
		},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 120 * time.Second},
	).Get(ctx, &pollOut); err != nil {
		return outbound.PollResult{}, err
	}

	if pollOut.Closed {
		return outbound.PollResult{NextCursor: pollOut.NextOffset, Closed: true}, nil
	}

	var deltas []outbound.Delta
	for _, item := range pollOut.Items {
		if item.Topic != turnEventsTopic {
			continue
		}
		turnNumber, event, err := decodeTurnEvent(item)
		if err != nil {
			workflow.GetLogger(ctx).Warn("PollTurn: decodeTurnEvent failed", "error", err)
			continue
		}
		if turnNumber < int(handle.TurnNumber) {
			continue
		}
		if delta := turnEventToDelta(*event); delta != nil {
			deltas = append(deltas, *delta)
		}
	}

	return outbound.PollResult{Deltas: deltas, NextCursor: pollOut.NextOffset}, nil
}
