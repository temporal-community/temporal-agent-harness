package router

import "go.temporal.io/sdk/workflow"

// OutboundDriver is implemented by a platform-specific workflow-side adapter and called
// directly by RouterWorkflow. Concrete drivers durably dispatch platform I/O to
// activity implementations (for example, SlackPlatform or the Python Teams worker).
type OutboundDriver interface {
	// SupportsStreaming reports whether the inbound conversation can receive
	// incremental response updates.
	SupportsStreaming(input Input) bool

	// BeginStream opens a response stream before any text deltas are delivered.
	// It returns the durable handle that must be passed to subsequent updates and
	// finalization. Drivers may emulate streaming by updating a single message.
	BeginStream(ctx workflow.Context, input BeginStreamInput) (StreamHandle, error)

	// UpdateStream delivers one text delta to the open stream.
	UpdateStream(ctx workflow.Context, input UpdateStreamInput) error

	// FinishStream closes the open stream. The router will not send more updates
	// for this handle afterward.
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
	Handle StreamHandle
	Delta  string
}

type FinishStreamInput struct {
	TextMetadata
	Handle StreamHandle
}

// BackendDriver is implemented by a concrete backend integration (e.g. the Nexus-based
// agent driver) and called directly by RouterWorkflow. All backend
// interpretation - including what a slash command means, or how an approval decision
// is resolved - lives behind this interface; router never inspects Input itself.
type BackendDriver interface {
	// StartTurn dispatches input (i.e., a message, slash command, or approval decision) to
	// the backend. See StartResult for how the router interprets the outcome.
	StartTurn(ctx workflow.Context, input Input) (StartResult, error)

	// PollTurn returns the next batch of deltas for a turn started via StartTurn.
	// Only called when StartResult.Handle was set. Call repeatedly, feeding
	// NextCursor back in as cursor, until Closed is true.
	PollTurn(ctx workflow.Context, handle TurnHandle, cursor int64) (PollResult, error)
}

// StartResult is the outcome of StartTurn. At most one field is populated:
//   - Reply set: an immediate, synchronous answer was produced (e.g. a harness
//     operator command); post it and stop - no turn was created.
//   - Handle set: a turn was created; poll it via PollTurn.
//   - neither set: nothing further to do (e.g. an approval decision was resolved
//     fire-and-forget).
type StartResult struct {
	Reply  string
	Handle *TurnHandle
}

// TurnHandle correlates a started turn with its response stream. It carries whatever a
// driver's own PollTurn implementation needs to resume polling - SessionID here because
// this driver's backend keys turns by session.
type TurnHandle struct {
	SessionID        string
	TurnID           string
	TurnNumber       int64
	StreamHeadOffset int64
}

// PollResult is one batch of a turn's response stream.
type PollResult struct {
	Deltas     []Delta
	NextCursor int64
	Closed     bool
}

// Delta is one backend-agnostic unit of turn output.
type Delta struct {
	Text              string
	IsFinal           bool
	ApprovalRequested *ApprovalRequest // non-nil if this delta is a tool-approval gate
}

// ApprovalRequest signals that a tool call is gated pending a human decision. This
// flows backend → router → outbound (router asks the outbound driver to prompt a human);
// it is distinct from ApprovalDecision, which flows the other way (the human's answer,
// inbound → router → backend).
type ApprovalRequest struct {
	ToolID        string
	ToolName      string
	ToolInputJSON string
}
