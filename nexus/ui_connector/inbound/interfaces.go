// Package inbound implements how to respond to inbound messages. The intent is for
// an inbound driver to implement a SignalWithStart (or similar) to start a RouterWorkflow,
// after which the RouterWorkflow will invoke the outbound driver to respond to the inbound request.
//
// Once the request is handled by the outbound side, the RouterWorkflow will reach for the APIs
// defined by this inbound.Driver interface to deliver the response back to the inbound side durably.
package inbound

import "go.temporal.io/sdk/workflow"

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

	// AcknowledgeApproval updates the inbound interaction after its decision is resolved.
	AcknowledgeApproval(ctx workflow.Context, input ApprovalAcknowledgementInput) error
}

// ApprovalPromptInput carries the information needed to render a tool-approval
// prompt (approve/deny buttons) on the messaging platform.
type ApprovalPromptInput struct {
	TextMetadata
	ToolID    string
	ToolName  string
	ToolInput string // JSON-encoded model-facing input (for display)
}

// ApprovalAcknowledgementInput carries a resolved approval decision back to the
// inbound platform. Each driver decides whether and how to update its prompt.
type ApprovalAcknowledgementInput struct {
	TextMetadata
	PromptID string
	ToolName string
	Approved bool
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

// StreamHandle is durable provider and routing state returned by BeginStream
// and passed to later stream calls.
type StreamHandle struct {
	ID                  string
	SessionID           string
	TransportMode       string
	TaskQueue           string
	CloseBeforeApproval bool
}

type BeginStreamInput struct {
	TextMetadata
	ConversationType string
}

type UpdateStreamInput struct {
	TextMetadata
	Handle   StreamHandle
	Delta    string
	FullText string
}

type FinishStreamInput struct {
	TextMetadata
	Handle   StreamHandle
	FullText string
}
