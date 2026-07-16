// Package inbound implements how to respond to inbound messages. The intent is for
// an inbound driver to implement a SignalWithStart (or similar) to start a RouterWorkflow,
// after which the RouterWorkflow will invoke the outbound driver to respond to the inbound request.
//
// Once the request is handled by the outbound side, the RouterWorkflow will reach for the APIs
// defined by this inbound.Driver interface to deliver the response back to the inbound side durably.
package inbound

import "go.temporal.io/sdk/workflow"

// Driver is implemented by a concrete platform driver and called directly by
// RouterWorkflow.
type Driver interface {
	// Stream starts, appends to, or finalises a streaming bot response.
	// DeltaTypeStart opens a new stream (StreamID must be empty) and returns its ID.
	// DeltaTypeAppend and DeltaTypeEnd require a non-empty StreamID; the returned
	// streamID echoes back the input StreamID.
	Stream(ctx workflow.Context, input StreamInput) (streamID string, err error)

	// PostMessage sends a single, non-streamed message.
	PostMessage(ctx workflow.Context, input TextMetadata) error

	// PostApprovalPrompt posts a tool-approval prompt with Approve/Deny buttons.
	// The decision comes back via the messaging platform's interaction webhook, not
	// through this interface.
	PostApprovalPrompt(ctx workflow.Context, input ApprovalPromptInput) error
}

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

// StreamInput is passed to Stream for each phase of a streaming response. DeltaType
// determines the phase; StreamID must be empty for Start and non-empty for
// Append/End. The platform driver maps these phases to its own lifecycle internally.
type StreamInput struct {
	TextMetadata
	StreamID  string
	DeltaType DeltaType
}
