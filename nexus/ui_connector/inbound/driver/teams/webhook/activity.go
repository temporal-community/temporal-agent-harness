package webhook

import "encoding/json"

// teamMessageActivity contains only the incoming Bot Framework fields needed
// by the webhook. Outbound activity models live in the Python Teams SDK worker.
type teamMessageActivity struct {
	Type         string                   `json:"type"`
	ID           string                   `json:"id,omitempty"`
	ReplyToID    string                   `json:"replyToId,omitempty"`
	Timestamp    string                   `json:"timestamp,omitempty"`
	ServiceURL   string                   `json:"serviceUrl,omitempty"`
	ChannelID    string                   `json:"channelId,omitempty"`
	From         *teamChannelAccount      `json:"from,omitempty"`
	Conversation *teamConversationAccount `json:"conversation,omitempty"`
	Text         string                   `json:"text,omitempty"`
	Value        json.RawMessage          `json:"value,omitempty"`
}

type teamChannelAccount struct {
	ID string `json:"id,omitempty"`
}

type teamConversationAccount struct {
	ID               string `json:"id,omitempty"`
	ConversationType string `json:"conversationType,omitempty"`
}

// approvalButtonValue is the compact state embedded in the Python worker's
// Adaptive Card Action.Submit data.
type approvalButtonValue struct {
	SessionID string `json:"s"`
	ToolID    string `json:"t"`
	ToolName  string `json:"n"`
	Approved  bool   `json:"a"`
}
