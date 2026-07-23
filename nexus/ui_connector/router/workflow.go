package router

import (
	"fmt"
	"strings"

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

	case result.Handle != nil && !supportsStreaming(input):
		// Teams channels and group chats do not support native streaming. Collect
		// the complete response and post it as a single message.
		return w.postResp(ctx, *result.Handle, input)

	case result.Handle != nil:
		// A turn was created; consume its response stream and deliver it inbound.
		return w.streamResp(ctx, *result.Handle, input)

	default:
		// Fire-and-forget (e.g. an approval decision was resolved) - nothing further.
		return nil
	}
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

// supportsStreaming reports whether the inbound conversation can receive
// incremental response updates.
func supportsStreaming(input wire.Input) bool {
	if input.Message == nil {
		return true
	}
	provider, _, found := strings.Cut(input.SessionID, ":")
	if !found || !strings.EqualFold(provider, "teams") {
		return true
	}
	switch strings.ToLower(strings.TrimSpace(input.Message.ConversationType)) {
	case "channel", "groupchat":
		return false
	default:
		return true
	}
}

// postResp polls a turn to completion and posts all text as one message. Teams
// channels and group chats use this path because they do not support native streams.
func (w *RouterWorkflow) postResp(ctx workflow.Context, handle outbound.TurnHandle, input wire.Input) error {
	cursor := handle.StreamHeadOffset
	fullText := ""

	for {
		res, err := w.outbound.PollTurn(ctx, handle, cursor)
		if err != nil {
			workflow.GetLogger(ctx).Warn("postResp: PollTurn failed", "error", err)
			return nil
		}
		cursor = res.NextCursor

		if res.Closed {
			if fullText == "" {
				return nil
			}
			return w.inbound.PostMessage(ctx, textMetadata(input, fullText))
		}

		for _, delta := range res.Deltas {
			if delta.ApprovalRequested != nil {
				req := delta.ApprovalRequested
				if err := w.inbound.PostApprovalPrompt(ctx, inbound.ApprovalPromptInput{
					TextMetadata: textMetadata(input, ""),
					ToolID:       req.ToolID,
					ToolName:     req.ToolName,
					ToolInput:    req.ToolInputJSON,
				}); err != nil {
					workflow.GetLogger(ctx).Warn("postResp: PostApprovalPrompt failed", "error", err)
				}
				continue
			}

			fullText += delta.Text
			if delta.IsFinal {
				if fullText == "" {
					return nil
				}
				return w.inbound.PostMessage(ctx, textMetadata(input, fullText))
			}
		}
	}
}

// streamResp polls the outbound driver for a started turn and streams each delta back
// through the inbound driver, until the turn closes. This loop is generic over any
// outbound/inbound pairing: it only deals in outbound.Delta and inbound.Driver calls.
func (w *RouterWorkflow) streamResp(ctx workflow.Context, handle outbound.TurnHandle, input wire.Input) error {
	cursor := handle.StreamHeadOffset

	// Start the initial inbound stream before polling.
	streamHandle, err := w.beginStream(ctx, input)
	if err != nil {
		workflow.GetLogger(ctx).Error("streamResp: stream begin failed", "error", err)
		return nil
	}

	for {
		res, err := w.outbound.PollTurn(ctx, handle, cursor)
		if err != nil {
			workflow.GetLogger(ctx).Warn("streamResp: PollTurn failed", "error", err)
			w.endStream(ctx, input, streamHandle)
			return nil
		}
		cursor = res.NextCursor

		// A turn may close without an explicit final delta.
		if res.Closed {
			w.endStream(ctx, input, streamHandle)
			return nil
		}

		for _, delta := range res.Deltas {
			if delta.ApprovalRequested != nil {
				req := delta.ApprovalRequested
				// Teams must finish the current stream before posting an approval card
				// so the messages appear in order. Clearing the handle makes the next text
				// delta start a new stream; Slack leaves this flag false.
				if streamHandle != nil && streamHandle.CloseBeforeApproval {
					w.endStream(ctx, input, streamHandle)
					streamHandle = nil
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
				// response text resumes.
				if streamHandle == nil {
					streamHandle, err = w.beginStream(ctx, input)
					if err != nil {
						workflow.GetLogger(ctx).Warn("streamResp: stream begin failed", "error", err)
						return nil
					}
				}
				w.updateStream(ctx, input, streamHandle, delta.Text)
			}

			if delta.IsFinal {
				w.endStream(ctx, input, streamHandle)
				return nil
			}
		}
	}
}

func (w *RouterWorkflow) beginStream(ctx workflow.Context, input wire.Input) (*inbound.StreamHandle, error) {
	conversationType := ""
	if input.Message != nil {
		conversationType = input.Message.ConversationType
	}
	streamHandle, err := w.inbound.BeginStream(ctx, inbound.BeginStreamInput{
		TextMetadata:     textMetadata(input, ""),
		ConversationType: conversationType,
	})
	if err != nil {
		return nil, err
	}
	return &streamHandle, nil
}

func (w *RouterWorkflow) updateStream(
	ctx workflow.Context,
	input wire.Input,
	handle *inbound.StreamHandle,
	delta string,
) {
	if handle == nil {
		return
	}
	if err := w.inbound.UpdateStream(ctx, inbound.UpdateStreamInput{
		TextMetadata: textMetadata(input, ""),
		Handle:       *handle,
		Delta:        delta,
	}); err != nil {
		workflow.GetLogger(ctx).Warn("streamResp: stream update failed", "error", err)
	}
}

func (w *RouterWorkflow) endStream(ctx workflow.Context, input wire.Input, handle *inbound.StreamHandle) {
	if handle == nil {
		return
	}
	if err := w.inbound.FinishStream(ctx, inbound.FinishStreamInput{
		TextMetadata: textMetadata(input, ""),
		Handle:       *handle,
	}); err != nil {
		workflow.GetLogger(ctx).Warn("streamResp: stream finish failed", "error", err)
	}
}
