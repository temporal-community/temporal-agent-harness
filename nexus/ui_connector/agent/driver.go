// Package agent implements router.Backend for the temporal-agent-harness Nexus service.
package agent

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	harnessgen "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/agent/generated"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
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

// derefOrZero returns the field value, or its zero value if the pointer is nil.
func derefOrZero[T any](p *T) T {
	if p == nil {
		var zero T
		return zero
	}
	return *p
}

func ptr[T any](v T) *T { return &v }

// streamItem matches the turn event payload written by WorkflowStream._log.
type streamItem struct {
	TurnID     string    `json:"turn_id"`
	TurnNumber int       `json:"turn_number"`
	MessageID  string    `json:"message_id"`
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
	Output     map[string]any `json:"output"`
	Approved   bool           `json:"approved"`
	Remember   bool           `json:"remember"`
	Reason     string         `json:"reason"`
}

func decodeTurnEvent(item harnessgen.StreamItem) (*streamItem, error) {
	b, err := base64.StdEncoding.DecodeString(item.Data)
	if err != nil {
		b, err = base64.URLEncoding.DecodeString(item.Data)
		if err != nil {
			return nil, fmt.Errorf("base64: %w", err)
		}
	}
	var payload commonpb.Payload
	if err := proto.Unmarshal(b, &payload); err != nil {
		return nil, fmt.Errorf("unmarshal Payload: %w", err)
	}
	var si streamItem
	if err := json.Unmarshal(payload.Data, &si); err != nil {
		return nil, fmt.Errorf("unmarshal streamItem: %w", err)
	}
	return &si, nil
}

// turnEventToEvent converts a harness event. It returns nil if the event has no
// output. final is true when the reply ends. A message-scoped reply ends at
// message_handler_end, not turn_end.
func turnEventToEvent(e turnEvent, messageScoped bool) (ev *protocol.Event, final bool) {
	chunk := func(c protocol.Chunk) *protocol.Event { return &protocol.Event{Chunk: &c} }
	switch e.Type {
	case "reply_delta":
		if e.Text != "" {
			return chunk(protocol.Chunk{Text: e.Text}), false
		}
	case "thought_summary":
		if text, ok := e.Delta["text"].(string); ok && text != "" {
			return chunk(protocol.Chunk{Thought: &protocol.ThoughtChunk{Delta: text}}), false
		}
	case "tool_start":
		return chunk(protocol.Chunk{Tool: &protocol.ToolChunk{ID: e.ToolID, Name: e.ToolName, Phase: protocol.ToolRunning, Input: toolInputJSON(e.ToolInput)}}), false
	case "tool_end":
		return chunk(protocol.Chunk{Tool: &protocol.ToolChunk{ID: e.ToolID, Name: e.ToolName, Phase: protocol.ToolComplete, Output: e.ToolOutput}}), false
	case "tool_error":
		return chunk(protocol.Chunk{Tool: &protocol.ToolChunk{ID: e.ToolID, Name: e.ToolName, Phase: protocol.ToolErrored, Error: e.Message}}), false
	case "text_annotation":
		if citations := extractCitations(e.Delta); len(citations) > 0 {
			return chunk(protocol.Chunk{Citations: citations}), false
		}
	case "turn_end":
		return nil, !messageScoped
	case "message_handler_end":
		return &protocol.Event{HandlerOutput: &protocol.HandlerOutput{Value: e.Output}}, messageScoped
	case "message_handler_error":
		return &protocol.Event{Error: "The agent's handler failed: " + e.Message}, messageScoped
	case "tool_approval_requested":
		return &protocol.Event{ApprovalRequested: &protocol.ApprovalRequest{
			ToolID:        e.ToolID,
			ToolName:      e.ToolName,
			ToolInputJSON: toolInputJSON(e.ToolInput),
		}}, false
	case "tool_approval_resolved":
		return &protocol.Event{ApprovalResolved: &protocol.ApprovalResolution{
			ToolID:   e.ToolID,
			ToolName: e.ToolName,
			Approved: e.Approved,
			Remember: e.Remember,
			Reason:   e.Reason,
		}}, false
	}
	return nil, false
}

// toolInputJSON returns the tool arguments as JSON, or "" when there are none.
func toolInputJSON(input map[string]any) string {
	if len(input) == 0 {
		return ""
	}
	b, err := json.Marshal(input)
	if err != nil {
		return ""
	}
	return string(b)
}

// extractCitations decodes a text_annotation event's annotations into protocol.Citations.
// URL falls back from custom_metadata.deep_url to document_uri; title from
// custom_metadata.heading, then .title, then file_name, then "Source".
func extractCitations(delta map[string]any) []protocol.Citation {
	raw, _ := delta["annotations"].([]any)
	citations := make([]protocol.Citation, 0, len(raw))
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

		citations = append(citations, protocol.Citation{URL: url, Title: title, EndIndex: endIndex})
	}
	return citations
}

// Driver implements router.Backend with the harness Nexus agent service.
type Driver struct {
	// NexusEndpoint selects the agent service endpoint. Empty uses AgentNexusEndpoint.
	NexusEndpoint string

	// ExistingSessionQueryTimeout bounds the session probe for a message that needs an
	// existing session. Zero uses DefaultExistingSessionQueryTimeout.
	ExistingSessionQueryTimeout time.Duration
}

var _ router.Backend = (*Driver)(nil)

func (d *Driver) nexusEndpoint() string {
	if d.NexusEndpoint == "" {
		return AgentNexusEndpoint
	}
	return d.NexusEndpoint
}

func (d *Driver) existingSessionQueryTimeout() time.Duration {
	if d.ExistingSessionQueryTimeout == 0 {
		return DefaultExistingSessionQueryTimeout
	}
	return d.ExistingSessionQueryTimeout
}

func (d *Driver) client() workflow.NexusClient {
	return workflow.NewNexusClient(d.nexusEndpoint(), harnessgen.AgentService.ServiceName)
}

// turnHandle is the router.Handle of a running turn.
type turnHandle struct {
	SessionID  string `json:"sessionId"`
	TurnNumber int64  `json:"turnNumber"`
	// MessageID scopes the stream to this message, so a message that joins an open
	// turn shows only its own events.
	MessageID string `json:"messageId,omitempty"`
	Cursor    int64  `json:"cursor"`
}

const noSessionReply = "No agent session exists yet. Send a chat message first; then use /help to list supported requests."

func reply(text string) router.Start {
	return router.Start{Events: []protocol.Event{{Chunk: &protocol.Chunk{Text: text}}}}
}

// Start sends the request to the agent.
func (d *Driver) Start(ctx workflow.Context, req protocol.Request) (router.Start, error) {
	c := d.client()
	switch {
	case req.Message != nil:
		return d.startMessage(ctx, c, req.SessionID, req.Message)

	case req.Call != nil:
		return startHandler(ctx, c, req.SessionID, req.Call.Name, req.Call.Payload)

	case req.OpenForm != nil:
		handlers, err := queryHandlers(ctx, c, req.SessionID)
		if err != nil {
			return reply(noSessionReply), nil
		}
		if h := findHandler(handlers, req.OpenForm.Handler); h != nil {
			return router.Start{Events: []protocol.Event{{Form: h}}}, nil
		}
		return reply(unknownHandlerReply(req.OpenForm.Handler, handlers)), nil

	case req.Approval != nil:
		return resolveApproval(ctx, c, req.SessionID, req.Approval), nil
	}
	return router.Start{}, nil
}

// startMessage reads a chat message: plain text goes to "ask", "/help" returns the
// guide, and "/name args" calls that handler.
func (d *Driver) startMessage(ctx workflow.Context, c workflow.NexusClient, sessionID string, m *protocol.Message) (router.Start, error) {
	if m.RequiresExistingSession {
		// Deliver a message without a mention only to an existing session.
		var statusOut harnessgen.AgentStatusOutput
		if err := c.ExecuteOperation(ctx, harnessgen.AgentService.QueryAgentStatus,
			harnessgen.QuerySessionInput{SessionId: sessionID},
			workflow.NexusOperationOptions{ScheduleToCloseTimeout: d.existingSessionQueryTimeout()},
		).Get(ctx, &statusOut); err != nil {
			return router.Start{}, nil
		}
	}

	in := parseIntent(m.Text)
	switch in.Kind {
	case intentGuide:
		handlers, err := queryHandlers(ctx, c, sessionID)
		if err != nil {
			return reply(noSessionReply), nil
		}
		return router.Start{Events: []protocol.Event{{Guide: &protocol.Guide{Handlers: handlers}}}}, nil

	case intentHandler:
		return handlerFromText(ctx, c, sessionID, in.Handler, in.Args)
	}

	payload, _ := json.Marshal(map[string]string{"text": in.Text})
	result, err := startHandler(ctx, c, sessionID, "ask", string(payload))
	if err != nil {
		return result, err
	}
	if m.FirstContact {
		// Deliver the reply even if the guide query fails.
		if handlers, err := queryHandlers(ctx, c, sessionID); err == nil {
			result.Events = append([]protocol.Event{{Guide: &protocol.Guide{Handlers: handlers}}}, result.Events...)
		} else {
			workflow.GetLogger(ctx).Warn("Start: could not fetch the first-contact guide", "error", err)
		}
	}
	return result, nil
}

// handlerFromText parses arguments and returns field errors for invalid input.
func handlerFromText(ctx workflow.Context, c workflow.NexusClient, sessionID, name, args string) (router.Start, error) {
	handlers, err := queryHandlers(ctx, c, sessionID)
	if err != nil {
		return reply(noSessionReply), nil
	}
	h := findHandler(handlers, name)
	if h == nil {
		return reply(unknownHandlerReply(name, handlers)), nil
	}
	if args == "" && len(h.Fields) > 0 {
		return router.Start{Events: []protocol.Event{{HandlerHelp: &protocol.HandlerHelp{Handler: *h}}}}, nil
	}
	payload, errs := protocol.Decode(h.Fields, parseText(h.Fields, args))
	if len(errs) > 0 {
		return router.Start{Events: []protocol.Event{{HandlerHelp: &protocol.HandlerHelp{Handler: *h, Errors: errs}}}}, nil
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return router.Start{}, err
	}
	return startHandler(ctx, c, sessionID, name, string(body))
}

func unknownHandlerReply(name string, handlers []protocol.Handler) string {
	names := make([]string, len(handlers))
	for i, h := range handlers {
		names[i] = h.Name
	}
	return fmt.Sprintf("Unknown handler %q. This agent accepts: %s", name, strings.Join(names, ", "))
}

func findHandler(handlers []protocol.Handler, name string) *protocol.Handler {
	for i := range handlers {
		if handlers[i].Name == name {
			return &handlers[i]
		}
	}
	return nil
}

// queryHandlers returns the session's handlers. It fails when the session does not
// exist yet.
func queryHandlers(ctx workflow.Context, c workflow.NexusClient, sessionID string) ([]protocol.Handler, error) {
	var out harnessgen.AgentInterfaceOutput
	if err := c.ExecuteOperation(ctx, harnessgen.AgentService.QueryAgentInterface,
		harnessgen.QuerySessionInput{SessionId: sessionID},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 10 * time.Second},
	).Get(ctx, &out); err != nil {
		return nil, err
	}
	handlers := make([]protocol.Handler, len(out.Handlers))
	for i, h := range out.Handlers {
		handlers[i] = protocol.Handler{
			Name:        h.Name,
			Description: h.Description,
			MidTurn:     string(derefOrZero(h.MidTurn)),
		}
		if fields, err := planFields(h.Parameters); err == nil {
			handlers[i].Fields = fields
		}
	}
	return handlers, nil
}

// startHandler sends one message to a handler and returns the handle of its turn.
func startHandler(ctx workflow.Context, c workflow.NexusClient, sessionID, msgType, payload string) (router.Start, error) {
	var sendOut harnessgen.SendMessageOutput
	if err := c.ExecuteOperation(ctx, harnessgen.AgentService.SendAgentMessage,
		harnessgen.SendAgentMessageInput{SessionId: sessionID, MsgType: msgType, Payload: payload},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 60 * time.Second},
	).Get(ctx, &sendOut); err != nil {
		return router.Start{}, err
	}
	handle, err := json.Marshal(turnHandle{
		SessionID:  sessionID,
		TurnNumber: sendOut.TurnNumber,
		MessageID:  derefOrZero(sendOut.MessageId),
		Cursor:     derefOrZero(sendOut.StreamHeadOffset),
	})
	if err != nil {
		return router.Start{}, err
	}
	return router.Start{Events: []protocol.Event{{TurnStarted: true}}, Handle: handle}, nil
}

// resolveApproval sends the decision and returns it as an event, so the outbound
// driver can update the prompt that was clicked.
func resolveApproval(ctx workflow.Context, c workflow.NexusClient, sessionID string, a *protocol.ApprovalDecision) router.Start {
	in := harnessgen.ApproveToolCallInput{SessionId: sessionID, ToolId: a.ToolID, Approved: a.Approved}
	if a.Remember && a.Approved {
		in.Remember = ptr(true)
	}
	if a.Reason != "" {
		in.Reason = ptr(a.Reason)
	}
	var out harnessgen.ApproveToolCallOutput
	if err := c.ExecuteOperation(ctx, harnessgen.AgentService.ApproveToolCall, in,
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 30 * time.Second},
	).Get(ctx, &out); err != nil {
		workflow.GetLogger(ctx).Warn("Start: approveToolCall failed",
			"toolId", a.ToolID, "approved", a.Approved, "error", err)
	}
	return router.Start{Events: []protocol.Event{{ApprovalResolved: &protocol.ApprovalResolution{
		ToolID: a.ToolID, ToolName: a.ToolName, Approved: a.Approved, Remember: a.Remember, Reason: a.Reason,
	}}}}
}

// Poll reads the next items of the session stream and converts them to events.
func (d *Driver) Poll(ctx workflow.Context, handle router.Handle) (router.Poll, error) {
	var h turnHandle
	if err := json.Unmarshal(handle, &h); err != nil {
		return router.Poll{}, fmt.Errorf("decode handle: %w", err)
	}
	var pollOut harnessgen.PollMessagesOutput
	if err := d.client().ExecuteOperation(ctx, harnessgen.AgentService.PollMessages,
		harnessgen.PollMessagesInput{
			SessionId:      h.SessionID,
			Cursor:         h.Cursor,
			TimeoutSeconds: ptr(5.0),
		},
		workflow.NexusOperationOptions{ScheduleToCloseTimeout: 120 * time.Second},
	).Get(ctx, &pollOut); err != nil {
		return router.Poll{}, err
	}
	if derefOrZero(pollOut.Closed) {
		return router.Poll{Done: true}, nil
	}

	messageScoped := h.MessageID != ""
	var events []protocol.Event
	for _, item := range pollOut.Items {
		if item.Topic != turnEventsTopic {
			continue
		}
		si, err := decodeTurnEvent(item)
		if err != nil {
			workflow.GetLogger(ctx).Warn("Poll: decodeTurnEvent failed", "error", err)
			continue
		}
		if messageScoped {
			// Keep events for this message and shared events with no message ID.
			if si.MessageID != "" && si.MessageID != h.MessageID {
				continue
			}
		} else if int64(si.TurnNumber) != h.TurnNumber {
			// Events of a later turn belong to the router workflow that started it.
			continue
		}
		ev, final := turnEventToEvent(si.Event, messageScoped)
		if ev != nil {
			events = append(events, *ev)
		}
		if final {
			return router.Poll{Events: events, Done: true}, nil
		}
	}

	h.Cursor = pollOut.NextOffset
	next, err := json.Marshal(h)
	if err != nil {
		return router.Poll{}, err
	}
	return router.Poll{Events: events, Handle: next}, nil
}
