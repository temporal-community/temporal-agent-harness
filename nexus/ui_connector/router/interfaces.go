package router

import (
	"errors"
	"time"

	"go.temporal.io/sdk/workflow"
)

// Poster sends a single, non-streamed message.
type Poster interface {
	PostMessage(ctx workflow.Context, input TextMetadata) error
}

// ApprovalHandler posts a tool-approval prompt and updates it once a decision resolves.
// The decision itself comes back through the platform's own interaction webhook, not
// through this interface.
type ApprovalHandler interface {
	PostApprovalPrompt(ctx workflow.Context, input ApprovalPromptInput) error
	AcknowledgeApproval(ctx workflow.Context, input ApprovalAcknowledgementInput) error
}

// Streamer delivers a reply as a live, incrementally-updated message instead of one
// final post.
type Streamer interface {
	// SupportsStreaming reports whether this input can stream. May vary per input -
	// e.g. Teams streams a personal chat but not a shared channel/group one.
	SupportsStreaming(input Input) bool

	// BeginStream opens a stream. Router only calls this when SupportsStreaming is true.
	BeginStream(ctx workflow.Context, input BeginStreamInput) (StreamHandle, error)

	// UpdateStream delivers one text delta to the open stream.
	UpdateStream(ctx workflow.Context, input UpdateStreamInput) error

	// FinishStream closes the stream. No more updates follow for this handle.
	FinishStream(ctx workflow.Context, input FinishStreamInput) error

	// StreamPollInterval is the wait between poll calls while a stream is open. Zero:
	// forward every delta right away. Non-zero: batch text into fewer, larger
	// UpdateStream calls - use this when UpdateStream is expensive per call (e.g.
	// Slack's rate-limited chat.appendStream). ToolStatus/ThoughtSummary/
	// ApprovalRequested deltas always flush immediately, never batched.
	StreamPollInterval(input Input) time.Duration
}

// OutboundDriver is the full contract a platform driver implements.
type OutboundDriver interface {
	Poster
	ApprovalHandler
	Streamer
}

// NoStreaming is Streamer for a driver that can't stream. Embed it to skip writing the
// five Streamer methods yourself:
//
//	type Driver struct {
//	    router.NoStreaming
//	    // ... your fields
//	}
//
// Router never calls BeginStream/UpdateStream/FinishStream when SupportsStreaming is
// false, so their bodies here are unreachable in normal use.
type NoStreaming struct{}

var _ Streamer = NoStreaming{}

func (NoStreaming) SupportsStreaming(Input) bool { return false }

func (NoStreaming) BeginStream(workflow.Context, BeginStreamInput) (StreamHandle, error) {
	return StreamHandle{}, errors.New("router: streaming not supported")
}

func (NoStreaming) UpdateStream(workflow.Context, UpdateStreamInput) error {
	return errors.New("router: streaming not supported")
}

func (NoStreaming) FinishStream(workflow.Context, FinishStreamInput) error {
	return errors.New("router: streaming not supported")
}

func (NoStreaming) StreamPollInterval(Input) time.Duration { return 0 }

// ApprovalPromptInput renders a tool-approval prompt (approve/deny buttons).
type ApprovalPromptInput struct {
	TextMetadata
	ToolID    string
	ToolName  string
	ToolInput string // JSON, for display
}

// ApprovalAcknowledgementInput is a resolved approval decision, for updating the prompt.
type ApprovalAcknowledgementInput struct {
	TextMetadata
	PromptID string
	ToolName string
	Approved bool
}

type TextMetadata struct {
	SenderID  string
	SessionID string
	ThreadID  string
	Text      string
	Citations []Citation
	// Segments is every delta of this message/turn, in order. Drivers with no richer
	// way to show tool status than inline text render it from here (see
	// slackoutbound/teamsoutbound flattenSegments).
	Segments   []Delta
	ServiceURL string
	ChannelID  string
}

// Citation is a source reference for part of a reply. EndIndex is -1 if unknown.
type Citation struct {
	URL      string
	Title    string
	EndIndex int
}

// UpdateMessageInput replaces an existing platform message.
type UpdateMessageInput struct {
	TextMetadata
	MessageID string
}

// StreamHandle is durable state returned by BeginStream and passed to later stream calls.
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
	Handle         StreamHandle
	Delta          string // reply text chunk; empty if this carries ToolStatus/ThoughtSummary instead
	ToolStatus     *ToolStatus
	ThoughtSummary string
}

type FinishStreamInput struct {
	TextMetadata
	Handle StreamHandle
}

// BackendDriver is the agent integration a router talks to (e.g. Nexus into
// temporal-agent-harness). It owns all input interpretation; router forwards Input
// unexamined.
type BackendDriver interface {
	// StartTurn dispatches input (message, slash command, or approval decision).
	StartTurn(ctx workflow.Context, input Input) (StartResult, error)

	// PollTurn returns the next batch of deltas. Call repeatedly with NextCursor until
	// Closed is true.
	PollTurn(ctx workflow.Context, handle TurnHandle, cursor int64) (PollResult, error)
}

// StartResult is the outcome of StartTurn. At most one field is set:
//   - Reply: a synchronous answer - post it, no turn was created.
//   - Handle: a turn was created; poll it.
//   - neither: nothing further to do.
type StartResult struct {
	Reply  string
	Handle *TurnHandle
}

// TurnHandle correlates a started turn with its response stream.
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

// Delta is one unit of turn output. Exactly one of Text, ToolStatus, ThoughtSummary, or
// ApprovalRequested is meaningfully populated; Citations may ride along or arrive alone.
type Delta struct {
	Text              string // the only field citations are indexed against
	Citations         []Citation
	ToolStatus        *ToolStatus
	ThoughtSummary    string
	IsFinal           bool
	ApprovalRequested *ApprovalRequest
}

// ToolStatusKind is the lifecycle state a ToolStatus delta reports.
type ToolStatusKind string

const (
	ToolStarted   ToolStatusKind = "started"
	ToolCompleted ToolStatusKind = "completed"
	ToolErrored   ToolStatusKind = "errored"
)

// ToolStatus is one tool call's lifecycle transition. ToolID ties started to
// completed/errored.
type ToolStatus struct {
	ToolID   string
	ToolName string
	Status   ToolStatusKind
	Message  string // set when Status is ToolErrored
}

// ApprovalRequest gates a tool call pending a human decision. Flows backend -> router ->
// outbound. ApprovalDecision is the reply, flowing the other way.
type ApprovalRequest struct {
	ToolID        string
	ToolName      string
	ToolInputJSON string
}
