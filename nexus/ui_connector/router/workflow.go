package router

import (
	"fmt"
	"time"

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

	if input.Approval != nil && input.Approval.ActivityID != "" {
		decision := "❌ Denied"
		if input.Approval.Approved {
			decision = "✅ Approved"
		}
		if err := w.inbound.UpdateActivity(ctx, inbound.UpdateActivityInput{
			TextMetadata: textMetadata(input, fmt.Sprintf("🔐 Tool `%s`: %s", input.Approval.ToolName, decision)),
			ActivityID:   input.Approval.ActivityID,
		}); err != nil {
			workflow.GetLogger(ctx).Warn("RouterWorkflow: UpdateActivity failed", "error", err)
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

type deliveryState struct {
	Handle       *inbound.StreamHandle
	FullText     string
	PendingDelta string
	LastFlush    time.Time
	Segment      int
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
	turnID := handle.TurnID
	if turnID == "" {
		turnID = fmt.Sprintf("turn-%d", handle.TurnNumber)
	}
	workflowID := workflow.GetInfo(ctx).WorkflowExecution.ID
	state := deliveryState{}

	operationID := func(phase string, sequence int) string {
		return fmt.Sprintf("%s/%s/segment/%d/%s/%d", workflowID, turnID, state.Segment, phase, sequence)
	}

	beginStream := func() error {
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
			OperationID:      operationID("begin", 0),
		})
		if err != nil {
			return err
		}
		state.Handle = &streamHandle
		state.LastFlush = workflow.Now(ctx)
		return nil
	}

	flushUpdate := func(force bool) error {
		if state.Handle == nil || state.PendingDelta == "" {
			return nil
		}
		now := workflow.Now(ctx)
		if !force && state.Handle.MinUpdateInterval > 0 && now.Sub(state.LastFlush) < state.Handle.MinUpdateInterval {
			return nil
		}
		sequence := state.Handle.NextSequence
		if err := w.inbound.UpdateStream(ctx, inbound.UpdateStreamInput{
			TextMetadata: textMetadata(input, ""),
			Handle:       *state.Handle,
			Delta:        state.PendingDelta,
			FullText:     state.FullText,
			Sequence:     sequence,
			OperationID:  operationID("update", sequence),
		}); err != nil {
			workflow.GetLogger(ctx).Warn("streamResp: stream update failed", "error", err)
			return err
		}
		state.PendingDelta = ""
		if state.Handle.NextSequence > 0 {
			state.Handle.NextSequence++
		}
		state.LastFlush = workflow.Now(ctx)
		return nil
	}

	finishStream := func() {
		if state.Handle == nil {
			return
		}
		if state.Handle.WireTextMode == inbound.StreamWireTextDelta {
			_ = flushUpdate(true)
		}
		if err := w.inbound.FinishStream(ctx, inbound.FinishStreamInput{
			TextMetadata: textMetadata(input, ""),
			Handle:       *state.Handle,
			FullText:     state.FullText,
			OperationID:  operationID("finish", state.Handle.NextSequence),
		}); err != nil {
			workflow.GetLogger(ctx).Warn("streamResp: stream finish failed", "error", err)
		}
	}

	resetForNextSegment := func() {
		state = deliveryState{Segment: state.Segment + 1}
	}

	for {
		res, err := w.outbound.PollTurn(ctx, handle, cursor)
		if err != nil {
			workflow.GetLogger(ctx).Warn("streamResp: PollTurn failed", "error", err)
			finishStream()
			return nil
		}
		cursor = res.NextCursor

		if res.Closed {
			finishStream()
			return nil
		}

		for _, delta := range res.Deltas {
			if delta.ApprovalRequested != nil {
				req := delta.ApprovalRequested
				if state.Handle != nil && state.Handle.CloseBeforeApproval {
					finishStream()
					resetForNextSegment()
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
				state.FullText += delta.Text
				state.PendingDelta += delta.Text
				if err := beginStream(); err != nil {
					workflow.GetLogger(ctx).Warn("streamResp: stream begin failed, falling back to postMessage", "error", err)
					_ = w.inbound.PostMessage(ctx, textMetadata(input, state.FullText))
					return nil
				}
				_ = flushUpdate(false)
			}

			if delta.IsFinal {
				finishStream()
				return nil
			}
		}
	}
}
