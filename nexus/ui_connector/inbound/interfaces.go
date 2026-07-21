// Package inbound implements how to respond to inbound messages. The intent is for
// an inbound driver to implement a SignalWithStart (or similar) to start a RouterWorkflow,
// after which the RouterWorkflow will invoke the outbound driver to respond to the inbound request.
//
// Once the request is handled by the outbound side, the RouterWorkflow will reach for the APIs
// defined by this inbound.Driver interface to deliver the response back to the inbound side durably.
package inbound

import (
	"time"

	"go.temporal.io/sdk/workflow"
)

// Driver is implemented by a platform-specific workflow-side adapter and called
// directly by RouterWorkflow. Concrete drivers durably dispatch platform I/O to
// activity implementations (for example, SlackPlatform or the Python Teams worker).
type Driver interface {
	BeginStream(ctx workflow.Context, input BeginStreamInput) (StreamHandle, error)
	UpdateStream(ctx workflow.Context, input UpdateStreamInput) error
	FinishStream(ctx workflow.Context, input FinishStreamInput) error

	// PostMessage sends a single, non-streamed message.
	PostMessage(ctx workflow.Context, input TextMetadata) error

	// PostApprovalPrompt posts a tool-approval prompt with Approve/Deny buttons.
	// The decision comes back via the messaging platform's interaction webhook, not
	// through this interface.
	PostApprovalPrompt(ctx workflow.Context, input ApprovalPromptInput) error

	// UpdateMessage replaces an existing platform message.
	UpdateMessage(ctx workflow.Context, input UpdateMessageInput) error
}

// ApprovalPromptInput carries the information needed to render a tool-approval
// prompt (approve/deny buttons) on the messaging platform.
type ApprovalPromptInput struct {
	TextMetadata
	ToolID    string
	ToolName  string
	ToolInput string // JSON-encoded model-facing input (for display)
}

type TextMetadata struct {
	SenderID   string
	SessionID  string
	ThreadID   string
	Text       string
	ServiceURL string
	ChannelID  string
}

// UpdateMessageInput replaces an existing platform message.
type UpdateMessageInput struct {
	TextMetadata
	MessageID string
}

// StreamWireTextMode tells the router which text representation a platform
// expects on update calls. Agent output is delta-driven for every platform;
// Slack sends the pending delta while Teams sends accumulated full text.
type StreamWireTextMode string

const (
	StreamWireTextDelta    StreamWireTextMode = "delta"
	StreamWireTextFullText StreamWireTextMode = "full_text"
)

// StreamHandle is durable provider state returned by BeginStream and passed to
// later stream calls. The router persists it in workflow state, so activities
// never rely on process-local maps.
type StreamHandle struct {
	ID                  string
	SessionID           string
	TransportMode       string
	WireTextMode        StreamWireTextMode
	MinUpdateInterval   time.Duration
	CloseBeforeApproval bool
	NextSequence        int
}

type BeginStreamInput struct {
	TextMetadata
	ConversationType string
	OperationID      string
}

type UpdateStreamInput struct {
	TextMetadata
	Handle      StreamHandle
	Delta       string
	FullText    string
	Sequence    int
	OperationID string
}

type FinishStreamInput struct {
	TextMetadata
	Handle      StreamHandle
	FullText    string
	OperationID string
}
