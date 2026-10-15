// Package router runs RouterWorkflow: it sends a request to a backend and delivers
// the backend's events to an outbound driver.
package router

import (
	"fmt"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"go.temporal.io/sdk/workflow"
)

// WorkflowName is the registered name of RouterWorkflow.Run.
const WorkflowName = "RouterWorkflow"

// RouterWorkflow connects one backend to one outbound driver.
type RouterWorkflow struct {
	outbound Outbound
	backend  Backend
}

// RouterWorkflowID returns a stable workflow ID for an interaction. interactionID is
// the platform's ID for the event.
func RouterWorkflowID(identity, sessionID, interactionID string) string {
	return RouterWorkflowIDPrefix(identity, sessionID) + interactionID
}

// RouterWorkflowIDPrefix returns the prefix used to find workflows for a session.
func RouterWorkflowIDPrefix(identity, sessionID string) string {
	return fmt.Sprintf("connector-%s-%s-", identity, sessionID)
}

func NewRouterWorkflow(outbound Outbound, backend Backend) *RouterWorkflow {
	return &RouterWorkflow{outbound: outbound, backend: backend}
}

// Run sends the request, then delivers events until the backend is done.
func (w *RouterWorkflow) Run(ctx workflow.Context, input Input) error {
	log := workflow.GetLogger(ctx)
	var state State
	deliver := func(events []protocol.Event) {
		if len(events) == 0 {
			return
		}
		next, err := w.outbound.Deliver(ctx, input.Address, state, events)
		if err != nil {
			// Keep the last saved state. The next delivery continues from it.
			log.Warn("RouterWorkflow: Deliver failed", "error", err)
			return
		}
		state = next
	}
	defer func() {
		if err := w.outbound.Done(ctx, input.Address, state); err != nil {
			log.Warn("RouterWorkflow: Done failed", "error", err)
		}
	}()

	start, err := w.backend.Start(ctx, input.Request)
	if err != nil {
		log.Warn("RouterWorkflow: Start failed", "error", err)
		deliver([]protocol.Event{{Error: err.Error()}})
		return nil
	}
	deliver(start.Events)

	interval := w.pollInterval()
	for handle, first := start.Handle, true; !Empty(handle); first = false {
		if interval > 0 && !first {
			if err := workflow.Sleep(ctx, interval); err != nil {
				return nil
			}
		}
		res, err := w.backend.Poll(ctx, handle)
		if err != nil {
			log.Warn("RouterWorkflow: Poll failed", "error", err)
			return nil
		}
		deliver(res.Events)
		if res.Done {
			return nil
		}
		if !Empty(res.Handle) {
			handle = res.Handle
		}
	}
	return nil
}

func (w *RouterWorkflow) pollInterval() time.Duration {
	if p, ok := w.outbound.(Pacer); ok {
		return p.PollInterval()
	}
	return 0
}
