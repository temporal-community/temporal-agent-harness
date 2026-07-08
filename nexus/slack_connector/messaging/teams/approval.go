package teams

import (
	"encoding/json"

	msgiface "github.com/temporalio/nexus_connector_slack/messaging"
)

// adaptiveCardContentType is the Bot Framework attachment content type for
// Adaptive Cards.
const adaptiveCardContentType = "application/vnd.microsoft.card.adaptive"

// ApprovalButtonValue is embedded in each Approve/Deny Action.Submit's data
// object so the webhook can reconstruct the decision without server-side
// state. Mirrors the Slack driver's ApprovalButtonValue, including the
// compact single-letter keys.
type ApprovalButtonValue struct {
	SessionID string `json:"s"`
	ToolID    string `json:"t"`
	ToolName  string `json:"n"`
	Approved  bool   `json:"a"`
}

// buildApprovalCard renders a tool-approval prompt as an Adaptive Card with
// Approve/Deny Action.Submit buttons. Version 1.4 is the safe floor across
// Teams desktop, web, and mobile clients.
func buildApprovalCard(input msgiface.ApprovalPromptInput) (json.RawMessage, error) {
	approve := ApprovalButtonValue{
		SessionID: input.SessionID,
		ToolID:    input.ToolID,
		ToolName:  input.ToolName,
		Approved:  true,
	}
	deny := approve
	deny.Approved = false

	body := []map[string]any{
		{
			"type":   "TextBlock",
			"text":   "🔐 Tool approval required",
			"weight": "Bolder",
			"wrap":   true,
		},
		{
			"type": "FactSet",
			"facts": []map[string]string{
				{"title": "Tool", "value": input.ToolName},
			},
		},
	}
	if input.ToolInput != "" {
		body = append(body, map[string]any{
			"type":     "TextBlock",
			"text":     input.ToolInput,
			"wrap":     true,
			"fontType": "Monospace",
			"isSubtle": true,
		})
	}

	return json.Marshal(map[string]any{
		"type":    "AdaptiveCard",
		"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
		"version": "1.4",
		"body":    body,
		"actions": []map[string]any{
			{"type": "Action.Submit", "title": "✅ Approve", "data": approve},
			{"type": "Action.Submit", "title": "❌ Deny", "data": deny},
		},
	})
}
