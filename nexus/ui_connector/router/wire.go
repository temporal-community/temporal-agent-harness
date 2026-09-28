package router

import "github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"

// Input is the RouterWorkflow argument. The inbound driver writes it.
type Input struct {
	Request protocol.Request `json:"request"`
	Address Address          `json:"address"`
}
