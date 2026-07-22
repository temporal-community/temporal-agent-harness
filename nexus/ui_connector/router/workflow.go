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

	if approval := input.Approval; approval != nil {
		if err := w.inbound.AcknowledgeApproval(ctx, inbound.ApprovalAcknowledgementInput{
			TextMetadata: textMetadata(input, ""),
			PromptID:     approval.ActivityID,
			ToolName:     approval.ToolName,
			Approved:     approval.Approved,
		}); err != nil {
			workflow.GetLogger(ctx).Warn("RouterWorkflow: AcknowledgeApproval failed", "error", err)
		}
		return nil
	}

	switch {
	case result.Reply != "":
		// An immediate, synchronous answer - no turn was created, nothing to poll.
		return w.inbound.PostMessage(ctx, textMetadata(input, result.Reply))

	case result.Handle != nil:
		// A turn was created; consume its response stream and deliver it inbound.
		return w.streamResp(ctx, *result.Handle, input)

	default:
		// Fire-and-forget (e.g. an approval decision was resolved) - nothing further.
		return nil
	}
}

// deliveryState tracks a response segment: Handle routes its active platform
// stream, while FullText supports finalization or recovery.
type deliveryState struct {
	Handle   *inbound.StreamHandle
	FullText string
}

func (s *deliveryState) reset() {
	*s = deliveryState{}
}

func textMetadata(input wire.Input, text string) inbound.TextMetadata {
	metadata := inbound.TextMetadata{
		SessionID: input.SessionID,
		ThreadID:  input.ThreadID(),
		SenderID:  input.SenderID(),
		Text:      text,
	}
	if input.Message != nil {
		metadata.ServiceURL = input.Message.ServiceURL
		metadata.ChannelID = input.Message.ChannelID
	} else if input.Approval != nil {
		metadata.ServiceURL = input.Approval.ServiceURL
		metadata.ChannelID = input.Approval.ChannelID
	}
	return metadata
}

// streamResp polls the outbound driver for a started turn and streams each delta back
// through the inbound driver, until the turn closes. This loop is generic over any
// outbound/inbound pairing: it only deals in outbound.Delta and inbound.Driver calls.
func (w *RouterWorkflow) streamResp(ctx workflow.Context, handle outbound.TurnHandle, input wire.Input) error {
	cursor := handle.StreamHeadOffset
	state := deliveryState{}

	// Start the initial inbound stream before polling. If it cannot be opened,
	// keep polling and buffer the response for one complete PostMessage.
	fallbackToPostMessage := false
	if err := w.beginStream(ctx, input, &state); err != nil {
		workflow.GetLogger(ctx).Warn("streamResp: stream begin failed, falling back to postMessage", "error", err)
		fallbackToPostMessage = true
	}

	for {
		res, err := w.outbound.PollTurn(ctx, handle, cursor)
		if err != nil {
			workflow.GetLogger(ctx).Warn("streamResp: PollTurn failed", "error", err)
			// The response is incomplete, so do not post the buffered fallback text.
			w.endStream(ctx, input, &state)
			return nil
		}
		cursor = res.NextCursor

		// A turn may close without an explicit final delta. Complete whichever
		// delivery mode was selected when the stream began.
		if res.Closed {
			if fallbackToPostMessage && state.FullText != "" {
				_ = w.inbound.PostMessage(ctx, textMetadata(input, state.FullText))
			} else {
				w.endStream(ctx, input, &state)
			}
			return nil
		}

		for _, delta := range res.Deltas {
			if delta.ApprovalRequested != nil {
				req := delta.ApprovalRequested
				// Teams must finish the current stream before posting an approval card
				// so the messages appear in order. Resetting state makes the next text
				// delta start a new stream; Slack leaves this flag false.
				if state.Handle != nil && state.Handle.CloseBeforeApproval {
					w.endStream(ctx, input, &state)
					state.reset()
					fallbackToPostMessage = false
				}
				if err := w.inbound.PostApprovalPrompt(ctx, inbound.ApprovalPromptInput{
					TextMetadata: textMetadata(input, ""),
					ToolID:       req.ToolID,
					ToolName:     req.ToolName,
					ToolInput:    req.ToolInputJSON,
				}); err != nil {
					workflow.GetLogger(ctx).Warn("streamResp: PostApprovalPrompt failed", "error", err)
				}
				continue
			}

			if delta.Text != "" {
				// A Teams approval may have closed the previous stream. Reopen it when
				// response text resumes. An initial begin failure stays in fallback mode
				// instead of retrying BeginStream on every text delta.
				if !fallbackToPostMessage && state.Handle == nil {
					if err := w.beginStream(ctx, input, &state); err != nil {
						workflow.GetLogger(ctx).Warn("streamResp: stream begin failed, falling back to postMessage", "error", err)
						fallbackToPostMessage = true
					}
				}

				// Always build the complete segment. In fallback mode, skip streaming
				// updates and post FullText once the segment is complete.
				state.FullText += delta.Text
				if !fallbackToPostMessage {
					w.updateStream(ctx, input, &state, delta.Text)
				}
			}

			// IsFinal means FullText is complete and safe to deliver as a fallback.
			if delta.IsFinal {
				if fallbackToPostMessage && state.FullText != "" {
					_ = w.inbound.PostMessage(ctx, textMetadata(input, state.FullText))
				} else {
					w.endStream(ctx, input, &state)
				}
				return nil
			}
		}
	}
}

func (w *RouterWorkflow) beginStream(ctx workflow.Context, input wire.Input, state *deliveryState) error {
	if state.Handle != nil {
		return nil
	}
	conversationType := ""
	if input.Message != nil {
		conversationType = input.Message.ConversationType
	}
	streamHandle, err := w.inbound.BeginStream(ctx, inbound.BeginStreamInput{
		TextMetadata:     textMetadata(input, ""),
		ConversationType: conversationType,
	})
	if err != nil {
		return err
	}
	state.Handle = &streamHandle
	return nil
}

func (w *RouterWorkflow) updateStream(
	ctx workflow.Context,
	input wire.Input,
	state *deliveryState,
	delta string,
) {
	if state.Handle == nil {
		return
	}
	if err := w.inbound.UpdateStream(ctx, inbound.UpdateStreamInput{
		TextMetadata: textMetadata(input, ""),
		Handle:       *state.Handle,
		Delta:        delta,
		FullText:     state.FullText,
	}); err != nil {
		workflow.GetLogger(ctx).Warn("streamResp: stream update failed", "error", err)
	}
}

func (w *RouterWorkflow) endStream(ctx workflow.Context, input wire.Input, state *deliveryState) {
	if state.Handle == nil {
		return
	}
	if err := w.inbound.FinishStream(ctx, inbound.FinishStreamInput{
		TextMetadata: textMetadata(input, ""),
		Handle:       *state.Handle,
		FullText:     state.FullText,
	}); err != nil {
		workflow.GetLogger(ctx).Warn("streamResp: stream finish failed", "error", err)
	}
}
