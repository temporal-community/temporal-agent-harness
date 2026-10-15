// Package teams holds types shared by the Teams inbound and outbound drivers.
package teams

// Address is where a Teams reply goes. The inbound webhook writes it; the outbound
// driver reads it. The router does not read it.
type Address struct {
	SessionID        string `json:"sessionId"` // "teams:<conversation ID>"
	SenderID         string `json:"senderId,omitempty"`
	ThreadID         string `json:"threadId,omitempty"`
	ConversationType string `json:"conversationType,omitempty"`
	ServiceURL       string `json:"serviceUrl,omitempty"`
	ChannelID        string `json:"channelId,omitempty"`
	// ActivityID is the approval card that a click came from. It is updated with the
	// decision.
	ActivityID string `json:"activityId,omitempty"`
}
