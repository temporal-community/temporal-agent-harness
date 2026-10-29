package router

import (
	"encoding/json"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"go.temporal.io/sdk/workflow"
)

// Backend runs requests on an agent. The router does not read the handle.
type Backend interface {
	// Start sends the request. A nil Handle means no events follow.
	Start(ctx workflow.Context, req protocol.Request) (Start, error)
	// Poll returns the next events and the handle for the next call. A nil handle
	// keeps the current one.
	Poll(ctx workflow.Context, handle Handle) (Poll, error)
}

// Handle is the backend's state for one request, as JSON.
type Handle = json.RawMessage

type Start struct {
	Events []protocol.Event
	Handle Handle
}

type Poll struct {
	Events []protocol.Event
	Handle Handle
	Done   bool
}

// Outbound delivers events to a platform and owns the address and state formats.
type Outbound interface {
	// Deliver shows events and returns state for the next call.
	Deliver(ctx workflow.Context, address Address, state State, events []protocol.Event) (State, error)
	// Done ends the delivery: it closes open messages and clears status.
	Done(ctx workflow.Context, address Address, state State) error
}

// Address identifies where to show output. The inbound driver writes it.
type Address = json.RawMessage

// State is the outbound driver's state between calls.
type State = json.RawMessage

// Empty reports whether a handle, address, or state holds no value. A nil value
// that went through Temporal's data converter comes back as JSON null.
func Empty(raw json.RawMessage) bool {
	return len(raw) == 0 || string(raw) == "null"
}

// Pacer sets an optional delay between polls to collect larger batches.
type Pacer interface {
	PollInterval() time.Duration
}
