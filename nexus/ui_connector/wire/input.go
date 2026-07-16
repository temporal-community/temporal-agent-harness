// Package wire defines the request envelope shared across the connector: inbound
// drivers construct it, router forwards it unexamined, and outbound drivers interpret
// it. Keeping it here (rather than in router or outbound) lets router and outbound
// import the same type without importing each other.
package wire

// Input is the single input type for RouterWorkflow. Exactly one of Message, Slash, or
// Approval is non-nil; router dispatches purely by forwarding it to the outbound
// driver — it never inspects which field is set.
type Input struct {
	SessionID string `json:"sessionId"`
	Identity  string `json:"identity"`

	Message  *IncomingMessage  `json:"message,omitempty"`
	Slash    *SlashCommand     `json:"slash,omitempty"`
	Approval *ApprovalDecision `json:"approval,omitempty"`
}

// ThreadID returns the platform thread ID for reply threading, regardless of which
// interaction sub-field is set.
func (i Input) ThreadID() string {
	if i.Message != nil {
		return i.Message.Timestamp
	}
	if i.Slash != nil {
		return i.Slash.ThreadID
	}
	return ""
}

// SenderID returns the platform sender ID regardless of which sub-field is set.
func (i Input) SenderID() string {
	if i.Message != nil {
		return i.Message.Sender
	}
	if i.Slash != nil {
		return i.Slash.SenderID
	}
	return ""
}

// IncomingMessage carries the raw message delivered from the platform.
type IncomingMessage struct {
	MessageID string
	Sender    string
	Text      string
	Timestamp string
}

// SlashCommand carries a slash command invocation from the platform.
type SlashCommand struct {
	Name     string `json:"name"`     // command name without leading /
	Arg      string `json:"arg"`      // argument text (may be empty)
	ThreadID string `json:"threadId"` // non-empty when invoked inside a thread
	SenderID string `json:"senderId"`
}

// ApprovalDecision carries a tool-approval decision from an interactive prompt.
type ApprovalDecision struct {
	ToolID   string `json:"toolId"`
	ToolName string `json:"toolName"` // for display in the interaction response
	Approved bool   `json:"approved"`
}
