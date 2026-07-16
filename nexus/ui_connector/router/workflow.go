package router

import (
	"fmt"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/outbound"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/wire"
	"go.temporal.io/sdk/workflow"
)

// WorkflowName is the single registered name for RouterWorkflow.Run.
const WorkflowName = "RouterWorkflow"

// RouterWorkflow routes a single interaction between an inbound driver and an outbound driver.
// It is the single entry point for all connector interactions, and is responsible for
// forwarding input to the outbound driver and reacting to the shape of the result.
type RouterWorkflow struct {
	inbound  inbound.Driver
	outbound outbound.Driver
}

// RouterWorkflowID returns a stable, unique workflow ID for an interaction.
// interactionID should be the platform's own unique ID for the event (Slack
// message timestamp, trigger_id for slash commands, etc.).
func RouterWorkflowID(identity, sessionID, interactionID string) string {
	return fmt.Sprintf("connector-%s-%s-%s", identity, sessionID, interactionID)
}

func NewRouterWorkflow(inboundDriver inbound.Driver, outboundDriver outbound.Driver) *RouterWorkflow {
	return &RouterWorkflow{inbound: inboundDriver, outbound: outboundDriver}
}

// Run is the workflow that makes a single turn between inbound and outbound durable.
func (w *RouterWorkflow) Run(ctx workflow.Context, input wire.Input) error {
	result, err := w.outbound.StartTurn(ctx, input)
	if err != nil {
		workflow.GetLogger(ctx).Warn("RouterWorkflow: StartTurn failed", "error", err)
		return nil
	}

	switch {
	case result.Reply != "":
		// An immediate, synchronous answer - no turn was created, nothing to poll.
		return w.inbound.PostMessage(ctx, inbound.TextMetadata{
			SessionID: input.SessionID,
			ThreadID:  input.ThreadID(),
			Text:      result.Reply,
		})

	case result.Handle != nil:
		// A turn was created; consume its response stream and deliver it inbound.
		return w.streamResp(ctx, *result.Handle, input)

	default:
		// Fire-and-forget (e.g. an approval decision was resolved) - nothing further.
		return nil
	}
}

// streamResp polls the outbound driver for a started turn and streams each delta back
// through the inbound driver, until the turn closes. This loop is generic over any
// outbound/inbound pairing: it only deals in outbound.Delta and inbound.Driver calls.
func (w *RouterWorkflow) streamResp(ctx workflow.Context, handle outbound.TurnHandle, input wire.Input) error {
	cursor := handle.StreamHeadOffset
	var streamID string

	for {
		res, err := w.outbound.PollTurn(ctx, handle, cursor)
		if err != nil {
			workflow.GetLogger(ctx).Warn("streamResp: PollTurn failed", "error", err)
			w.endStream(ctx, input, streamID)
			return nil
		}
		cursor = res.NextCursor

		if res.Closed {
			w.endStream(ctx, input, streamID)
			return nil
		}

		for _, delta := range res.Deltas {
			if delta.ApprovalRequested != nil {
				req := delta.ApprovalRequested
				if err := w.inbound.PostApprovalPrompt(ctx, inbound.ApprovalPromptInput{
					SessionID: input.SessionID,
					ThreadID:  input.ThreadID(),
					ToolID:    req.ToolID,
					ToolName:  req.ToolName,
					ToolInput: req.ToolInputJSON,
				}); err != nil {
					workflow.GetLogger(ctx).Warn("streamResp: PostApprovalPrompt failed", "error", err)
				}
				continue
			}

			if delta.Text != "" {
				// streamID == "" iff this is the first delta, so we start the inbound
				// stream to get the ID for subsequent appends.
				if streamID == "" {
					sid, err := w.inbound.Stream(ctx, inbound.StreamInput{
						TextMetadata: inbound.TextMetadata{
							SessionID: input.SessionID,
							ThreadID:  input.ThreadID(),
							SenderID:  input.SenderID(),
						},
						DeltaType: inbound.DeltaTypeStart,
					})
					if err != nil {
						workflow.GetLogger(ctx).Warn("streamResp: stream start failed, falling back to postMessage", "error", err)
						_ = w.inbound.PostMessage(ctx, inbound.TextMetadata{
							SessionID: input.SessionID,
							ThreadID:  input.ThreadID(),
							Text:      delta.Text,
						})
						return nil
					}
					streamID = sid
				}

				if _, err := w.inbound.Stream(ctx, inbound.StreamInput{
					TextMetadata: inbound.TextMetadata{
						SessionID: input.SessionID,
						Text:      delta.Text,
					},
					StreamID:  streamID,
					DeltaType: inbound.DeltaTypeAppend,
				}); err != nil {
					workflow.GetLogger(ctx).Warn("streamResp: stream append failed", "error", err)
				}
			}

			if delta.IsFinal {
				w.endStream(ctx, input, streamID)
				return nil
			}
		}
	}
}

func (w *RouterWorkflow) endStream(ctx workflow.Context, input wire.Input, streamID string) {
	if streamID == "" {
		return
	}
	_, _ = w.inbound.Stream(ctx, inbound.StreamInput{
		TextMetadata: inbound.TextMetadata{SessionID: input.SessionID},
		StreamID:     streamID,
		DeltaType:    inbound.DeltaTypeEnd,
	})
}
