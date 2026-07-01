package messaging

import "context"

// Activity name constants for messaging platform activities.
// Used in both workflow code (ExecuteActivity) and worker registration (RegisterActivityWithOptions).
const (
	StreamActivity              = "Stream"
	PostMessageActivity         = "PostMessage"
	PostApprovalPromptActivity  = "PostApprovalPrompt"
)

// ApprovalPromptInput carries the information needed to render a tool-approval
// prompt (approve/deny buttons) on the messaging platform.
type ApprovalPromptInput struct {
	SessionID string
	ThreadID  string // empty = post in channel root; non-empty = post in thread
	ToolID    string
	ToolName  string
	ToolInput string // JSON-encoded model-facing input (for display)
}

type TextMetadata struct {
	SenderID  string
	SessionID string
	ThreadID  string
	Text      string
}

// DeltaType indicates which phase of the streaming lifecycle a Stream call represents.
type DeltaType int

const (
	DeltaTypeStart  DeltaType = iota // begins a new stream; StreamID must be empty
	DeltaTypeAppend                  // appends text to an existing stream; StreamID required
	DeltaTypeEnd                     // finalises an existing stream; StreamID required
)

// StreamInput is passed to the Stream activity for each phase of a streaming response.
// DeltaType determines the phase; StreamID must be empty for Start and non-empty for
// Append/End. The platform driver maps these phases to its own lifecycle internally.
type StreamInput struct {
	TextMetadata
	StreamID  string
	DeltaType DeltaType
}

// MessagingPlatform is the interface that messaging platform drivers must implement.
type MessagingPlatform interface {
	// Stream starts, appends to, or finalises a streaming bot response.
	// DeltaTypeStart opens a new stream (StreamID must be empty) and returns its ID.
	// DeltaTypeAppend and DeltaTypeEnd require a non-empty StreamID; the returned
	// streamID echoes back the input StreamID.
	Stream(ctx context.Context, input StreamInput) (streamID string, err error)
	PostMessage(ctx context.Context, input TextMetadata) error
	// PostApprovalPrompt posts a tool-approval prompt with Approve/Deny buttons.
	// The decision comes back via the messaging platform's interaction webhook.
	PostApprovalPrompt(ctx context.Context, input ApprovalPromptInput) error
}
