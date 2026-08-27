// Package agent implements router.BackendDriver against the
// temporal-agent-harness's Nexus agent service. This enables us to know
// how to route a message from the inbound driver to the temporal-agent-harness.
package agent

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"time"

	harnessgen "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/agent/generated"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"
)

const (
	// AgentNexusEndpoint is the Nexus endpoint name the driver targets.
	AgentNexusEndpoint = "support-agent-nexus"
	turnEventsTopic    = "turn_events"

	// DefaultExistingSessionQueryTimeout is used when
	// Driver.ExistingSessionQueryTimeout is unset.
	DefaultExistingSessionQueryTimeout = 30 * time.Second
)

// derefOrZero returns *p, or the zero value of T if p is nil. This utility
// helps us read optional fields from the IDL stubs, which are represented as
// pointer-typed fields in Go.
func derefOrZero[T any](p *T) T {
	if p == nil {
		var zero T
		return zero
	}
	return *p
}

// ptr returns a pointer to v, for populating our IDL stub's optional fields,
// which are represented as pointer-typed fields in Go.
func ptr[T any](v T) *T { return &v }

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

func decodeTurnEvent(item harnessgen.StreamItem) (int, *turnEvent, error) {
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

// turnEventToDelta maps one decoded turn event to a generic router.Delta, or nil if
// the event type carries no deliverable content.
func turnEventToDelta(e turnEvent) *router.Delta {
	switch e.Type {
	case "reply_delta":
		return &router.Delta{Text: e.Text}
	case "thought_summary":
		if text, ok := e.Delta["text"].(string); ok && text != "" {
			return &router.Delta{ThoughtSummary: text}
		}
	case "tool_start":
		return &router.Delta{ToolStatus: &router.ToolStatus{ToolID: e.ToolID, ToolName: e.ToolName, Status: router.ToolStarted}}
	case "tool_end":
		return &router.Delta{ToolStatus: &router.ToolStatus{ToolID: e.ToolID, ToolName: e.ToolName, Status: router.ToolCompleted}}
	case "tool_error":
		return &router.Delta{ToolStatus: &router.ToolStatus{ToolID: e.ToolID, ToolName: e.ToolName, Status: router.ToolErrored, Message: e.Message}}
	case "text_annotation":
		if citations := extractCitations(e.Delta); len(citations) > 0 {
			return &router.Delta{Citations: citations}
		}
	case "reply":
		// Text was already fully streamed via reply_delta events; this just signals completion.
		return &router.Delta{IsFinal: true}
	case "error":
		return &router.Delta{Text: "[error] " + e.Message, IsFinal: true}
	case "tool_approval_requested":
		// Tool approval gates surface as a delta with no text; the approval workflow
		// (started by the interaction webhook) later calls approveToolCall, which
		// StartTurn's Approval case resolves.
		inputJSON, _ := json.Marshal(e.ToolInput)
		return &router.Delta{ApprovalRequested: &router.ApprovalRequest{
			ToolID:        e.ToolID,
			ToolName:      e.ToolName,
			ToolInputJSON: string(inputJSON),
		}}
	}
	return nil
}

// extractCitations decodes a text_annotation event's annotations into router.Citations.
// URL falls back from custom_metadata.deep_url to document_uri; title from
// custom_metadata.heading, then .title, then file_name, then "Source".
func extractCitations(delta map[string]any) []router.Citation {
	raw, _ := delta["annotations"].([]any)
	citations := make([]router.Citation, 0, len(raw))
	for _, item := range raw {
		ann, ok := item.(map[string]any)
		if !ok {
			continue
		}
		meta, _ := ann["custom_metadata"].(map[string]any)

		url, _ := meta["deep_url"].(string)
		if url == "" {
			url, _ = ann["document_uri"].(string)
		}

		title, _ := meta["heading"].(string)
		if title == "" {
			title, _ = meta["title"].(string)
		}
		if title == "" {
			title, _ = ann["file_name"].(string)
		}
		if title == "" {
			title = "Source"
		}

		endIndex := -1
		if v, ok := ann["end_index"].(float64); ok {
			endIndex = int(v)
		}

		citations = append(citations, router.Citation{URL: url, Title: title, EndIndex: endIndex})
	}
	return citations
}

// Driver implements router.BackendDriver against the temporal-agent-harness's Nexus agent
// service.
type Driver struct {
	// NexusEndpoint is the Nexus endpoint name to target. Leave empty to use
	// AgentNexusEndpoint. Set this explicitly when running multiple
	// environments (e.g. prod/staging/ondemand) against the same Temporal
	// Cloud account — Nexus endpoint names must be unique account-wide, not
	// just namespace-wide, so each environment needs its own.
	NexusEndpoint string

	// ExistingSessionQueryTimeout bounds the QueryAgentStatus probe in
	// StartTurn's RequiresExistingSession branch. Leave zero to use
	// DefaultExistingSessionQueryTimeout.
	ExistingSessionQueryTimeout time.Duration
}

// nexusEndpoint returns d.NexusEndpoint, or AgentNexusEndpoint if unset.
func (d *Driver) nexusEndpoint() string {
	if d.NexusEndpoint == "" {
		return AgentNexusEndpoint
	}
	return d.NexusEndpoint
}

// existingSessionQueryTimeout returns d.ExistingSessionQueryTimeout, or
// DefaultExistingSessionQueryTimeout if unset.
func (d *Driver) existingSessionQueryTimeout() time.Duration {
	if d.ExistingSessionQueryTimeout == 0 {
		return DefaultExistingSessionQueryTimeout
	}
	return d.ExistingSessionQueryTimeout
}

// StartTurn dispatches a message, slash command, or approval decision to the agent
// nexus service, translating the backend's response into a generic router.StartResult.
func (d *Driver) StartTurn(ctx workflow.Context, input router.Input) (router.StartResult, error) {
	agentClient := workflow.NewNexusClient(d.nexusEndpoint(), harnessgen.AgentService.ServiceName)

	switch {
	case input.Message != nil:
		if input.Message.RequiresExistingSession {
			// Mention-free thread continuation: only proceed if the agent already has a
			// live session for this thread by probing with a query.
			var statusOut harnessgen.AgentStatusOutput
			if err := agentClient.ExecuteOperation(ctx, harnessgen.AgentService.QueryAgentStatus,
				harnessgen.QuerySessionInput{SessionId: input.SessionID},
				workflow.NexusOperationOptions{ScheduleToCloseTimeout: d.existingSessionQueryTimeout()},
			).Get(ctx, &statusOut); err != nil {
				return router.StartResult{}, nil
			}
		}

		payload := fmt.Sprintf(`{"text":%q}`, input.Message.Text)
		sendOut, err := sendAgentMessage(ctx, agentClient, input.SessionID, "ask", payload)
		if err != nil {
			return router.StartResult{}, err
		}
		return router.StartResult{Handle: &router.TurnHandle{
			SessionID:        input.SessionID,
			TurnID:           sendOut.TurnId,
			TurnNumber:       sendOut.TurnNumber,
			StreamHeadOffset: derefOrZero(sendOut.StreamHeadOffset),
		}}, nil

	case input.Slash != nil:
		return startSlashTurn(ctx, agentClient, input.SessionID, input.Slash)

	case input.Approval != nil:
		return resolveApproval(ctx, agentClient, input.SessionID, input.Approval)

	default:
		return router.StartResult{}, nil
	}
}

func sendAgentMessage(ctx workflow.Context, agentClient workflow.NexusClient, sessionID, msgType, payload string) (harnessgen.SendMessageOutput, error) {
	var sendOut harnessgen.SendMessageOutput
	err := agentClient.ExecuteOperation(ctx, harnessgen.AgentService.SendAgentMessage,
		harnessgen.SendAgentMessageInput{SessionId: sessionID, MsgType: msgType, Payload: payload},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 60 * time.Second},
	).Get(ctx, &sendOut)
	return sendOut, err
}

// startSlashTurn decides whether s names a harness-owned operator command (synchronous,
// no turn) or an agent-owned slash command (creates a turn), and dispatches accordingly.
// Agent-owned slash commands (@agent.accepts slash) are NOT in the operator interface -
// they route directly to sendAgentMessage(type="slash").
func startSlashTurn(ctx workflow.Context, agentClient workflow.NexusClient, sessionID string, s *router.SlashCommand) (router.StartResult, error) {
	var ifaceOut harnessgen.QueryOperatorInterfaceOutput
	if err := agentClient.ExecuteOperation(ctx, harnessgen.AgentService.QueryOperatorInterface,
		harnessgen.QuerySessionInput{SessionId: sessionID},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 10 * time.Second},
	).Get(ctx, &ifaceOut); err != nil {
		return router.StartResult{Reply: "_No active session. Start a conversation first before using slash commands._"}, nil
	}

	var cmd *harnessgen.OperatorCommand
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
			harnessgen.ExecuteOperatorCommandInput{SessionId: sessionID, Name: s.Name, Arg: &s.Arg},
			workflow.NexusOperationOptions{ScheduleToCloseTimeout: 30 * time.Second},
		).Get(ctx, &opOut); err != nil {
			return router.StartResult{Reply: fmt.Sprintf("_Command failed: %v_", err)}, nil
		}
		return router.StartResult{Reply: opOut.Reply}, nil
	}

	// Agent slash command: creates a turn, same as a message.
	payload, _ := json.Marshal(map[string]string{"name": s.Name, "arg": s.Arg})
	sendOut, err := sendAgentMessage(ctx, agentClient, sessionID, "slash", string(payload))
	if err != nil {
		return router.StartResult{Reply: fmt.Sprintf("_Command failed: %v_", err)}, nil
	}
	return router.StartResult{Handle: &router.TurnHandle{
		SessionID:        sessionID,
		TurnID:           sendOut.TurnId,
		TurnNumber:       sendOut.TurnNumber,
		StreamHeadOffset: derefOrZero(sendOut.StreamHeadOffset),
	}}, nil
}

func resolveApproval(ctx workflow.Context, agentClient workflow.NexusClient, sessionID string, a *router.ApprovalDecision) (router.StartResult, error) {
	var out harnessgen.ApproveToolCallOutput
	if err := agentClient.ExecuteOperation(ctx, harnessgen.AgentService.ApproveToolCall,
		harnessgen.ApproveToolCallInput{SessionId: sessionID, ToolId: a.ToolID, Approved: a.Approved},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 30 * time.Second},
	).Get(ctx, &out); err != nil {
		workflow.GetLogger(ctx).Warn("StartTurn: approveToolCall failed",
			"toolId", a.ToolID, "approved", a.Approved, "error", err)
	}
	return router.StartResult{}, nil
}

// PollTurn polls the Nexus agent response stream starting from cursor and decodes each
// item into a generic router.Delta.
func (d *Driver) PollTurn(ctx workflow.Context, handle router.TurnHandle, cursor int64) (router.PollResult, error) {
	agentClient := workflow.NewNexusClient(d.nexusEndpoint(), harnessgen.AgentService.ServiceName)

	var pollOut harnessgen.PollMessagesOutput
	if err := agentClient.ExecuteOperation(ctx, harnessgen.AgentService.PollMessages,
		harnessgen.PollMessagesInput{
			SessionId:      handle.SessionID,
			Cursor:         cursor,
			TimeoutSeconds: ptr(5.0),
		},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 120 * time.Second},
	).Get(ctx, &pollOut); err != nil {
		return router.PollResult{}, err
	}

	if derefOrZero(pollOut.Closed) {
		return router.PollResult{NextCursor: pollOut.NextOffset, Closed: true}, nil
	}

	var deltas []router.Delta
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

	return router.PollResult{Deltas: deltas, NextCursor: pollOut.NextOffset}, nil
}
