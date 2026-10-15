// Package interaction builds and decodes Slack buttons and forms.
package interaction

// Block Kit action and callback IDs. The inbound webhook dispatches on these.
const (
	ActionApprove        = "tool_approval_approve"
	ActionAlwaysAllow    = "tool_approval_always"
	ActionDeny           = "tool_approval_deny"
	ActionOpenFormPrefix = "open_form:"
	FormCallbackID       = "handler_form"
)

// ApprovalButtonValue carries the session and decision in each button value.
type ApprovalButtonValue struct {
	SessionID string `json:"s"`
	ToolID    string `json:"t"`
	ToolName  string `json:"n"`
	Approved  bool   `json:"a"`
	Remember  bool   `json:"r,omitempty"`
}
