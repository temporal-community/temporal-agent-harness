// Package outbound is what the router workflow will use to forward inbound messages to.
// It implements the "backend" that will handle the inbound request, and return a response
// for our router workflow to deliver back to our inbound driver.
package outbound

import (
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/wire"
	"go.temporal.io/sdk/workflow"
)

// Driver is implemented by a concrete backend integration (e.g. the Nexus-based
// temporal_agent_harness driver) and called directly by RouterWorkflow. All backend
// interpretation - including what a slash command means, or how an approval decision
// is resolved - lives behind this interface; router never inspects wire.Input itself.
type Driver interface {
	// StartTurn dispatches input (i.e., a message, slash command, or approval decision) to
	// the backend. See StartResult for how the router interprets the outcome.
	StartTurn(ctx workflow.Context, input wire.Input) (StartResult, error)

	// PollTurn returns the next batch of deltas for a turn started via StartTurn.
	// Only called when StartResult.Handle was set. Call repeatedly, feeding
	// NextCursor back in as cursor, until Closed is true.
	PollTurn(ctx workflow.Context, handle TurnHandle, cursor int64) (PollResult, error)
}

// StartResult is the outcome of StartTurn. At most one field is populated:
//   - Reply set: an immediate, synchronous answer was produced (e.g. a harness
//     operator command); post it and stop - no turn was created.
//   - Handle set: a turn was created; poll it via PollTurn.
//   - neither set: nothing further to do (e.g. an approval decision was resolved
//     fire-and-forget).
type StartResult struct {
	Reply  string
	Handle *TurnHandle
}

// TurnHandle correlates a started turn with its response stream. It carries whatever a
// driver's own PollTurn implementation needs to resume polling - SessionID here because
// this driver's backend keys turns by session.
type TurnHandle struct {
	SessionID        string
	TurnID           string
	TurnNumber       int64
	StreamHeadOffset int64
}

// PollResult is one batch of a turn's response stream.
type PollResult struct {
	Deltas     []Delta
	NextCursor int64
	Closed     bool
}

// Delta is one backend-agnostic unit of turn output.
type Delta struct {
	Text              string
	IsFinal           bool
	ApprovalRequested *ApprovalRequest // non-nil if this delta is a tool-approval gate
}

// ApprovalRequest signals that a tool call is gated pending a human decision. This
// flows outbound → router → inbound (router asks inbound to prompt a human); it is
// distinct from wire.ApprovalDecision, which flows the other way (the human's answer,
// inbound → router → outbound).
type ApprovalRequest struct {
	ToolID        string
	ToolName      string
	ToolInputJSON string
}
