package router

// The tunnel never stores these presentation types. They are optional helpers for
// harness-aware edge drivers which choose to render the harness extension carried
// by an otherwise standard A2A StreamResponse.

type TextMetadata struct {
	SenderID   string
	SessionID  string
	ThreadID   string
	Text       string
	Citations  []Citation
	Segments   []Delta
	ServiceURL string
	ChannelID  string
}

type Citation struct {
	URL      string
	Title    string
	EndIndex int
}

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
	Delta          string
	ToolStatus     *ToolStatus
	ThoughtSummary string
}

type FinishStreamInput struct {
	TextMetadata
	Handle StreamHandle
}

type ApprovalPromptInput struct {
	TextMetadata
	ToolID    string
	ToolName  string
	ToolInput string
}

type UpdateMessageInput struct {
	TextMetadata
	MessageID string
}

type Delta struct {
	Text              string
	Citations         []Citation
	ToolStatus        *ToolStatus
	ThoughtSummary    string
	IsFinal           bool
	ApprovalRequested *ApprovalRequest
}

type ToolStatusKind string

const (
	ToolStarted   ToolStatusKind = "started"
	ToolCompleted ToolStatusKind = "completed"
	ToolErrored   ToolStatusKind = "errored"
)

type ToolStatus struct {
	ToolID   string
	ToolName string
	Status   ToolStatusKind
	Message  string
}

type ApprovalRequest struct {
	ToolID        string
	ToolName      string
	ToolInputJSON string
}
