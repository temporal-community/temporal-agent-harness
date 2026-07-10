package agent

import (
	"fmt"

	"go.temporal.io/sdk/workflow"
)

// TurnHandle is the correlation token returned by ReceiveMessageFromPlatform and
// consumed by RespondToPlatform. It identifies the agent turn and the stream
// position from which the response should be consumed.
type TurnHandle struct {
	TurnNumber       int64 `json:"turnNumber"`
	StreamHeadOffset int64 `json:"streamHeadOffset"`
}

// MessageHandler is implemented by anything that can handle a single platform turn.
// The connector workflow calls the two methods in sequence for message and slash
// interactions; approval interactions are handled directly by the workflow.
type MessageHandler interface {
	ReceiveMessageFromPlatform(ctx workflow.Context, input ConnectorWorkflowInput) (TurnHandle, error)
	RespondToPlatform(ctx workflow.Context, handle TurnHandle, input ConnectorWorkflowInput) error
}

// IncomingMessage carries the raw message delivered from the platform.
type IncomingMessage struct {
	MessageID string
	Sender    string
	Text      string
	Timestamp string
	// ThreadID is the thread root the reply should be posted under: the thread's
	// parent ts for a threaded reply, or the message's own ts for a top-level
	// message (which starts a new thread). Also scopes the agent session.
	ThreadID string
}

// SlashCommand carries a slash command invocation from the platform.
type SlashCommand struct {
	Name     string `json:"name"`     // command name without leading /
	Arg      string `json:"arg"`      // argument text (may be empty)
	ThreadID string `json:"threadId"` // non-empty when invoked inside a thread
	SenderID string `json:"senderId"`
}

// ApprovalDecision carries a tool-approval decision from an interactive prompt.
type ApprovalDecision struct {
	ToolID   string `json:"toolId"`
	ToolName string `json:"toolName"` // for display in the interaction response
	Approved bool   `json:"approved"`
}

// ConnectorWorkflowInput is the single input type for ConnectorWorkflow.
// Exactly one of Message, Slash, or Approval is non-nil; the workflow dispatches
// on whichever field is set so the webhook never needs an explicit kind discriminant.
type ConnectorWorkflowInput struct {
	SessionID string `json:"sessionId"`
	Identity  string `json:"identity"`

	Message  *IncomingMessage  `json:"message,omitempty"`
	Slash    *SlashCommand     `json:"slash,omitempty"`
	Approval *ApprovalDecision `json:"approval,omitempty"`
}

// ThreadID returns the platform thread ID for reply threading, regardless of
// which interaction sub-field is set.
func (i ConnectorWorkflowInput) ThreadID() string {
	if i.Message != nil {
		return i.Message.ThreadID
	}
	if i.Slash != nil {
		return i.Slash.ThreadID
	}
	return ""
}

// SenderID returns the platform sender ID regardless of which sub-field is set.
func (i ConnectorWorkflowInput) SenderID() string {
	if i.Message != nil {
		return i.Message.Sender
	}
	if i.Slash != nil {
		return i.Slash.SenderID
	}
	return ""
}

// ConnectorWorkflowID returns a stable, unique workflow ID for an interaction.
// interactionID should be the platform's own unique ID for the event (Slack
// message timestamp, trigger_id for slash commands, etc.).
func ConnectorWorkflowID(identity, sessionID, interactionID string) string {
	return fmt.Sprintf("connector-%s-%s-%s", identity, sessionID, interactionID)
}
