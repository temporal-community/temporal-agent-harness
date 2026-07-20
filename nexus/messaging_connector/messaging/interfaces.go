package messaging

import (
	"context"
	"time"
)

// Activity name constants for messaging platform activities.
// Used in both workflow code (ExecuteActivity) and worker registration (RegisterActivityWithOptions).
const (
	BeginStreamActivity        = "BeginStream"
	UpdateStreamActivity       = "UpdateStream"
	FinishStreamActivity       = "FinishStream"
	PostMessageActivity        = "PostMessage"
	PostApprovalPromptActivity = "PostApprovalPrompt"
	UpdateActivityActivity     = "UpdateActivity"
)

// ApprovalPromptInput carries the information needed to render a tool-approval
// prompt (approve/deny buttons) on the messaging platform.
type ApprovalPromptInput struct {
	SessionID  string
	ThreadID   string // empty = post in channel root; non-empty = post in thread
	ServiceURL string
	ChannelID  string
	ToolID     string
	ToolName   string
	ToolInput  string // JSON-encoded model-facing input (for display)
}

type TextMetadata struct {
	SenderID   string
	SessionID  string
	ThreadID   string
	Text       string
	ServiceURL string
	ChannelID  string
}

// UpdateActivityInput replaces an existing platform activity. Teams uses it
// to remove approval buttons after a decision has been recorded.
type UpdateActivityInput struct {
	TextMetadata
	ActivityID string
}

// StreamWireTextMode tells the workflow which text representation a platform
// expects on update calls. Agent output is delta-driven for every platform;
// Slack sends the pending delta while Teams sends the accumulated full text.
type StreamWireTextMode string

const (
	StreamWireTextDelta    StreamWireTextMode = "delta"
	StreamWireTextFullText StreamWireTextMode = "full_text"
)

// StreamHandle is durable provider state returned by BeginStream and passed to
// later stream activities. TransportMode is interpreted only by the platform
// adapter; the remaining fields drive platform-neutral workflow behavior.
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

// MessagingPlatform is the interface that messaging platform drivers must implement.
type MessagingPlatform interface {
	BeginStream(ctx context.Context, input BeginStreamInput) (StreamHandle, error)
	UpdateStream(ctx context.Context, input UpdateStreamInput) error
	FinishStream(ctx context.Context, input FinishStreamInput) error
	PostMessage(ctx context.Context, input TextMetadata) error
	// PostApprovalPrompt posts a tool-approval prompt with Approve/Deny buttons.
	// The decision comes back via the messaging platform's interaction webhook.
	PostApprovalPrompt(ctx context.Context, input ApprovalPromptInput) error
}
