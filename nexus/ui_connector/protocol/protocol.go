// Package protocol defines the requests and events that inbound drivers, backends,
// and outbound drivers exchange. The router carries these values and does not read
// them.
package protocol

// Request is a user action for the backend. Exactly one of Message, Call, OpenForm,
// or Approval is set.
type Request struct {
	SessionID string            `json:"sessionId"`
	Message   *Message          `json:"message,omitempty"`
	Call      *HandlerCall      `json:"call,omitempty"`
	OpenForm  *FormRequest      `json:"openForm,omitempty"`
	Approval  *ApprovalDecision `json:"approval,omitempty"`
}

// Message is a chat message from a user.
type Message struct {
	Text   string `json:"text"`
	Sender string `json:"sender"`
	// RequiresExistingSession permits delivery only to an existing session.
	RequiresExistingSession bool `json:"requiresExistingSession,omitempty"`
	// FirstContact asks for the handler guide before the reply.
	FirstContact bool `json:"firstContact,omitempty"`
}

// HandlerCall sends a JSON payload to a named handler.
type HandlerCall struct {
	Name    string `json:"name"`
	Payload string `json:"payload"` // JSON object that matches the handler's fields
}

// FormRequest asks for the input form of a handler.
type FormRequest struct {
	Handler string `json:"handler"`
}

// ApprovalDecision is a decision on a tool call that waits for approval.
type ApprovalDecision struct {
	ToolID   string `json:"toolId"`
	ToolName string `json:"toolName"`
	Approved bool   `json:"approved"`
	// Remember permits later calls to this tool in the session. Used with Approved.
	Remember bool `json:"remember,omitempty"`
	// Reason is shown on the decision. On denial the model also receives it.
	Reason string `json:"reason,omitempty"`
}

// Event is one unit of backend output. Exactly one field is set.
type Event struct {
	// TurnStarted tells that the agent started work on the request.
	TurnStarted       bool                `json:",omitempty"`
	Chunk             *Chunk              `json:",omitempty"`
	ApprovalRequested *ApprovalRequest    `json:",omitempty"`
	ApprovalResolved  *ApprovalResolution `json:",omitempty"`
	// HandlerOutput is the handler's return value. Show it only if the reply had no text.
	HandlerOutput *HandlerOutput `json:",omitempty"`
	// Guide lists the handlers the agent accepts.
	Guide *Guide `json:",omitempty"`
	// Form asks to open the input form of a handler.
	Form *Handler `json:",omitempty"`
	// HandlerHelp describes missing or invalid arguments for a handler.
	HandlerHelp *HandlerHelp `json:",omitempty"`
	// Error tells that the request failed.
	Error string `json:",omitempty"`
}

// Chunk is one piece of reply content. Exactly one field is set.
type Chunk struct {
	Text      string        `json:",omitempty"`
	Tool      *ToolChunk    `json:",omitempty"`
	Thought   *ThoughtChunk `json:",omitempty"`
	Citations []Citation    `json:",omitempty"`
}

// ToolPhase is the state of a tool call.
type ToolPhase string

const (
	ToolRunning  ToolPhase = "running"
	ToolComplete ToolPhase = "complete"
	ToolErrored  ToolPhase = "error"
)

// ToolChunk is the state of one tool call. ID is the same for all phases of a call.
type ToolChunk struct {
	ID     string
	Name   string
	Phase  ToolPhase
	Input  string `json:",omitempty"` // JSON arguments
	Output string `json:",omitempty"` // set when Phase is ToolComplete
	Error  string `json:",omitempty"` // set when Phase is ToolErrored
}

// ThoughtChunk is part of the model's thought summary. A block ends when other
// content arrives.
type ThoughtChunk struct {
	ID    string `json:",omitempty"` // stable for the block
	Delta string `json:",omitempty"` // text added since the last chunk of the block
	Text  string `json:",omitempty"` // full text of the block so far
	Done  bool   `json:",omitempty"` // the block ended; Delta is empty
}

// Citation is a source for part of the reply text. EndIndex is a rune offset into the
// reply text, or -1 if unknown.
type Citation struct {
	URL      string
	Title    string
	EndIndex int
}

// ApprovalRequest is a tool call that waits for a person to approve it.
type ApprovalRequest struct {
	ToolID        string
	ToolName      string
	ToolInputJSON string
}

// ApprovalResolution is a decision on a waiting tool call, from any source.
type ApprovalResolution struct {
	ToolID   string
	ToolName string
	Approved bool
	Remember bool
	Reason   string
}

// HandlerOutput is the structured value returned by a handler.
type HandlerOutput struct {
	Value map[string]any
}

// Guide lists the handlers an agent accepts.
type Guide struct {
	Handlers []Handler
}
