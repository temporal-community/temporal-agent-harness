package slack

// Address is where a Slack reply goes. The inbound webhook writes it; the outbound
// driver reads it. The router does not read it.
type Address struct {
	SessionID string `json:"sessionId"` // for buttons that send a decision back
	Channel   string `json:"channel"`
	ThreadTS  string `json:"threadTs,omitempty"`
	UserID    string `json:"userId,omitempty"`    // recipient of a streamed reply
	TriggerID string `json:"triggerId,omitempty"` // opens a modal; valid about 3 s
}
