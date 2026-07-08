package messaging

import (
	"context"
	"encoding/json"
)

// Activity name constants for messaging platform activities.
// Used in both workflow code (ExecuteActivity) and worker registration (RegisterActivityWithOptions).
const (
	StreamActivity             = "Stream"
	PostMessageActivity        = "PostMessage"
	PostApprovalPromptActivity = "PostApprovalPrompt"
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

// TeamMessageActivity is the Bot Framework Activity JSON shape used by Teams
// webhooks and outbound Teams message sends.
type TeamMessageActivity struct {
	Type         string                   `json:"type"`
	ID           string                   `json:"id,omitempty"`
	ReplyToID    string                   `json:"replyToId,omitempty"`
	Timestamp    string                   `json:"timestamp,omitempty"`
	ServiceURL   string                   `json:"serviceUrl,omitempty"`
	ChannelID    string                   `json:"channelId,omitempty"`
	From         *TeamChannelAccount      `json:"from,omitempty"`
	Conversation *TeamConversationAccount `json:"conversation,omitempty"`
	Recipient    *TeamChannelAccount      `json:"recipient,omitempty"`
	Text         string                   `json:"text,omitempty"`
	TextFormat   string                   `json:"textFormat,omitempty"`
	// Value carries an Adaptive Card Action.Submit's data object on incoming
	// button-click activities (delivered as type "message" with empty text).
	Value       json.RawMessage        `json:"value,omitempty"`
	Attachments []TeamAttachment       `json:"attachments,omitempty"`
	Entities    []TeamStreamInfoEntity `json:"entities,omitempty"`
}

// TeamAttachment is a Bot Framework Attachment, used to send rich cards
// (e.g. Adaptive Cards) as part of a Teams activity.
type TeamAttachment struct {
	ContentType string          `json:"contentType"`
	Content     json.RawMessage `json:"content,omitempty"`
}

type TeamChannelAccount struct {
	ID string `json:"id,omitempty"`
}

type TeamConversationAccount struct {
	ID string `json:"id,omitempty"`
}

type TeamStreamInfoEntity struct {
	Type           string `json:"type"`
	StreamID       string `json:"streamId,omitempty"`
	StreamType     string `json:"streamType,omitempty"`
	StreamSequence *int   `json:"streamSequence,omitempty"`
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
