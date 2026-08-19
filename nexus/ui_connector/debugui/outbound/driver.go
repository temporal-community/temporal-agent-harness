package debuguioutbound

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"go.temporal.io/sdk/workflow"
)

// Driver implements router.OutboundDriver for the debug UI. Unlike Slack/Teams, there is
// no external platform API to call: "delivery" means publishing the backend's own event
// (router.Delta.Payload, already the harness's native event shape) to whichever browser
// SSE connections are attached to this session, via Broker. Rendering is therefore almost
// nothing - the driver's whole job is forwarding, not translating, because the debug UI
// is built to show full fidelity rather than a flattened, platform-specific view.
type Driver struct {
	ActivityOptions workflow.ActivityOptions
}

var _ router.OutboundDriver = (*Driver)(nil)

// NewDriver returns a Driver that publishes through BrokerActivities.Publish with the
// given options.
func NewDriver(opts workflow.ActivityOptions) Driver {
	return Driver{ActivityOptions: opts}
}

// SupportsStreaming reports that the debug UI renders every delta incrementally, same as
// Slack/Teams - it just renders far more of each delta's content.
func (Driver) SupportsStreaming(router.Input) bool {
	return true
}

// BeginStream needs no real platform call - there's no external "stream" resource to
// open, only a browser SSE connection the HTTP layer already owns independently of this
// workflow. Returns a handle carrying just the session ID so later calls know which
// session's subscribers to publish to.
func (Driver) BeginStream(_ workflow.Context, input router.BeginStreamInput) (router.StreamHandle, error) {
	return router.StreamHandle{SessionID: input.SessionID}, nil
}

// UpdateStream publishes the delta's full event payload verbatim. Skips publishing when
// Payload is empty (defensive only - a real BackendDriver always sets it; see
// router.Delta's doc comment).
func (d Driver) UpdateStream(ctx workflow.Context, input router.UpdateStreamInput) error {
	if len(input.Payload) == 0 {
		return nil
	}
	return d.publish(ctx, input.Handle.SessionID, input.EventType, input.Payload)
}

// FinishStream needs no real platform call, for the same reason as BeginStream - turn
// completion is already signaled by whatever terminal event (reply, error) the backend
// published, which UpdateStream already forwarded.
func (Driver) FinishStream(_ workflow.Context, _ router.FinishStreamInput) error {
	return nil
}

// PostMessage delivers a synchronous, non-streamed reply - used for an immediate answer
// that never created a turn (e.g. a harness operator command). Router gives us only
// rendered text here, not a backend event, so this synthesizes the closest matching SSE
// shape (a generic "reply" event) rather than fabricating turn/agent IDs the backend
// never assigned.
func (d Driver) PostMessage(ctx workflow.Context, input router.TextMetadata) error {
	payload, err := json.Marshal(map[string]any{
		"type":        "reply",
		"text":        input.Text,
		"agent_id":    "",
		"turn_id":     "",
		"turn_number": 0,
		"timestamp":   float64(time.Now().Unix()),
	})
	if err != nil {
		return fmt.Errorf("marshal synthesized reply event: %w", err)
	}
	return d.publish(ctx, input.SessionID, "reply", payload)
}

// PostApprovalPrompt publishes the tool_approval_requested event verbatim, same as
// UpdateStream - it's already the harness's native event shape.
func (d Driver) PostApprovalPrompt(ctx workflow.Context, input router.ApprovalPromptInput) error {
	if len(input.Payload) == 0 {
		return nil
	}
	return d.publish(ctx, input.SessionID, "tool_approval_requested", input.Payload)
}

// AcknowledgeApproval is a no-op: the debug UI's session-scoped attach connection (see
// ../admin.AttachWorkflow) independently tails the same event stream and already
// delivers the resulting tool_approval_resolved event on its own, the same way Slack
// resolves its prompt "some other way" (see router.OutboundDriver's doc comment).
func (Driver) AcknowledgeApproval(_ workflow.Context, _ router.ApprovalAcknowledgementInput) error {
	return nil
}

func (d Driver) publish(ctx workflow.Context, sessionID, eventType string, payload json.RawMessage) error {
	return workflow.ExecuteActivity(
		workflow.WithActivityOptions(ctx, d.ActivityOptions),
		PublishActivityName,
		PublishInput{SessionID: sessionID, Frame: Frame{Event: eventType, Data: payload}},
	).Get(ctx, nil)
}
