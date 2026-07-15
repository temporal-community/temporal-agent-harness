package messaging

import (
	"context"
	"encoding/json"
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
	ID               string `json:"id,omitempty"`
	ConversationType string `json:"conversationType,omitempty"`
}

type TeamStreamInfoEntity struct {
	Type           string `json:"type"`
	StreamID       string `json:"streamId,omitempty"`
	StreamType     string `json:"streamType,omitempty"`
	StreamSequence *int   `json:"streamSequence,omitempty"`
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
