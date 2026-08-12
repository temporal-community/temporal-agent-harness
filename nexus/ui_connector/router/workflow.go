// Package router implements RouterWorkflow, the single Temporal workflow type shared by
// every inbound platform (Slack, Teams, ...). It defines the two ports platform code
// plugs into - OutboundDriver (delivers a response back to the user) and BackendDriver
// (produces the response) - plus the shared Input envelope inbound drivers construct to
// start it. Platform packages (slack/, teams/, agent/) import router to implement these
// ports; router never imports platform code.
package router

import (
	"fmt"

	"go.temporal.io/sdk/workflow"
)

// WorkflowName is the single registered name for RouterWorkflow.Run.
const WorkflowName = "RouterWorkflow"

// RouterWorkflow routes a single interaction between an outbound driver and a backend.
// It is the single entry point for all connector interactions, and is responsible for
// forwarding input to the backend and reacting to the shape of the result.
type RouterWorkflow struct {
	outbound OutboundDriver
	backend  BackendDriver
}

// RouterWorkflowID returns a stable, unique workflow ID for an interaction.
// interactionID should be the platform's own unique ID for the event (Slack
// message timestamp, trigger_id for slash commands, etc.).
func RouterWorkflowID(identity, sessionID, interactionID string) string {
	return RouterWorkflowIDPrefix(identity, sessionID) + interactionID
}

// RouterWorkflowIDPrefix returns the shared prefix of every RouterWorkflowID for a
// given session, with everything up to but excluding interactionID. Every
// interaction within one session (e.g. every reply in a Slack thread) produces a
// workflow ID with this same prefix, so it can be used to search for any workflow
// ever started for the session without knowing which interactionID triggered it.
func RouterWorkflowIDPrefix(identity, sessionID string) string {
	return fmt.Sprintf("connector-%s-%s-", identity, sessionID)
}

func NewRouterWorkflow(outboundDriver OutboundDriver, backendDriver BackendDriver) *RouterWorkflow {
	return &RouterWorkflow{outbound: outboundDriver, backend: backendDriver}
}

// Run is the workflow that makes a single turn between the outbound driver and the
// backend durable.
func (w *RouterWorkflow) Run(ctx workflow.Context, input Input) error {
	result, err := w.backend.StartTurn(ctx, input)
	if err != nil {
		workflow.GetLogger(ctx).Warn("RouterWorkflow: StartTurn failed", "error", err)
		return nil
	}

	if approval := input.Approval; approval != nil {
		if err := w.outbound.AcknowledgeApproval(ctx, ApprovalAcknowledgementInput{
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
		return w.outbound.PostMessage(ctx, textMetadata(input, result.Reply))

	case result.Handle != nil && !w.outbound.SupportsStreaming(input):
		// Collect the complete response when the outbound conversation does not
		// support native streaming, then post it as a single message.
		return w.postResp(ctx, *result.Handle, input)

	case result.Handle != nil:
		// A turn was created; consume its response stream and deliver it outbound.
		return w.streamResp(ctx, *result.Handle, input)

	default:
		// Fire-and-forget (e.g. an approval decision was resolved) - nothing further.
		return nil
	}
}

func textMetadata(input Input, text string) TextMetadata {
	metadata := TextMetadata{
		SessionID: input.SessionID,
		ThreadID:  input.ThreadID(),
		SenderID:  input.SenderID(),
		Text:      text,
	}
	// Segments defaults to a single reply-text delta so drivers that build their final
	// message from Segments (e.g. slackoutbound, teamsoutbound) still render text
	// delivered outside the polling loop, such as a synchronous slash-command reply.
	// Callers that poll a turn (postResp, streamResp) overwrite this with the real
	// per-delta sequence.
	if text != "" {
		metadata.Segments = []Delta{{Text: text}}
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

// postResp polls a turn to completion and posts all text as one message for
// outbound conversations that do not support native streams.
func (w *RouterWorkflow) postResp(ctx workflow.Context, handle TurnHandle, input Input) error {
	cursor := handle.StreamHeadOffset
	fullText := ""
	hasContent := false
	var citations []Citation
	var segments []Delta

	post := func() error {
		if !hasContent {
			return nil
		}
		metadata := textMetadata(input, fullText)
		metadata.Citations = citations
		metadata.Segments = segments
		return w.outbound.PostMessage(ctx, metadata)
	}

	for {
		res, err := w.backend.PollTurn(ctx, handle, cursor)
		if err != nil {
			workflow.GetLogger(ctx).Warn("postResp: PollTurn failed", "error", err)
			return nil
		}
		cursor = res.NextCursor

		if res.Closed {
			return post()
		}

		for _, delta := range res.Deltas {
			if delta.ApprovalRequested != nil {
				req := delta.ApprovalRequested
				if err := w.outbound.PostApprovalPrompt(ctx, ApprovalPromptInput{
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
			citations = append(citations, delta.Citations...)
			segments = append(segments, delta)
			if delta.Text != "" || delta.ToolStatus != nil || delta.ThoughtSummary != "" || len(delta.Citations) > 0 {
				hasContent = true
			}
			if delta.IsFinal {
				return post()
			}
		}
	}
}

// streamResp polls the backend for a started turn and streams each delta back
// through the outbound driver, until the turn closes. This loop is generic over any
// backend/outbound pairing: it only deals in Delta and OutboundDriver calls.
func (w *RouterWorkflow) streamResp(ctx workflow.Context, handle TurnHandle, input Input) error {
	cursor := handle.StreamHeadOffset

	// Start the initial outbound stream before polling. If the outbound driver can't
	// open a stream (e.g. its activity worker died mid-attempt with no retries left),
	// fall back to collecting the full response and posting it as one message rather
	// than silently dropping it.
	streamHandle, err := w.beginStream(ctx, input)
	if err != nil {
		workflow.GetLogger(ctx).Warn("streamResp: stream begin failed, falling back to postResp", "error", err)
		return w.postResp(ctx, handle, input)
	}

	// segmentText/segmentCitations/segments accumulate the current open stream's full
	// content, separately from the live per-delta UpdateStream calls below. Outbound
	// drivers that can only append while streaming (e.g. Slack's chat.appendStream) need
	// the full text, citations, and ordered deltas together, after the fact, to correct
	// the message once the segment ends - see endStream. Reset whenever a new segment
	// begins (initially, and after an approval closes the previous one).
	var segmentText string
	var segmentCitations []Citation
	var segments []Delta

	for {
		res, err := w.backend.PollTurn(ctx, handle, cursor)
		if err != nil {
			workflow.GetLogger(ctx).Warn("streamResp: PollTurn failed", "error", err)
			w.endStream(ctx, input, streamHandle, segmentText, segmentCitations, segments)
			return nil
		}
		cursor = res.NextCursor

		// A turn may close without an explicit final delta.
		if res.Closed {
			w.endStream(ctx, input, streamHandle, segmentText, segmentCitations, segments)
			return nil
		}

		for _, delta := range res.Deltas {
			if delta.ApprovalRequested != nil {
				req := delta.ApprovalRequested
				// Some outbound drivers must finish the current stream before posting an
				// approval card so the messages appear in order. Clearing the handle makes
				// the next text delta start a new stream.
				if streamHandle != nil && streamHandle.CloseBeforeApproval {
					w.endStream(ctx, input, streamHandle, segmentText, segmentCitations, segments)
					streamHandle = nil
					segmentText = ""
					segmentCitations = nil
					segments = nil
				}
				if err := w.outbound.PostApprovalPrompt(ctx, ApprovalPromptInput{
					TextMetadata: textMetadata(input, ""),
					ToolID:       req.ToolID,
					ToolName:     req.ToolName,
					ToolInput:    req.ToolInputJSON,
				}); err != nil {
					workflow.GetLogger(ctx).Warn("streamResp: PostApprovalPrompt failed", "error", err)
				}
				continue
			}

			segmentCitations = append(segmentCitations, delta.Citations...)
			segments = append(segments, delta)

			if delta.Text != "" || delta.ToolStatus != nil || delta.ThoughtSummary != "" {
				// An approval may have closed the previous stream. Reopen it when response
				// content resumes.
				if streamHandle == nil {
					streamHandle, err = w.beginStream(ctx, input)
					if err != nil {
						workflow.GetLogger(ctx).Warn("streamResp: stream begin failed", "error", err)
						return nil
					}
				}
				segmentText += delta.Text
				w.updateStream(ctx, input, streamHandle, delta)
			}

			if delta.IsFinal {
				w.endStream(ctx, input, streamHandle, segmentText, segmentCitations, segments)
				return nil
			}
		}
	}
}

func (w *RouterWorkflow) beginStream(ctx workflow.Context, input Input) (*StreamHandle, error) {
	conversationType := ""
	if input.Message != nil {
		conversationType = input.Message.ConversationType
	}
	streamHandle, err := w.outbound.BeginStream(ctx, BeginStreamInput{
		TextMetadata:     textMetadata(input, ""),
		ConversationType: conversationType,
	})
	if err != nil {
		return nil, err
	}
	return &streamHandle, nil
}

// updateStream forwards one delta's content to the outbound driver verbatim - it does
// not interpret Text/ToolStatus/ThoughtSummary; that's entirely the driver's call.
func (w *RouterWorkflow) updateStream(
	ctx workflow.Context,
	input Input,
	handle *StreamHandle,
	delta Delta,
) {
	if handle == nil {
		return
	}
	if err := w.outbound.UpdateStream(ctx, UpdateStreamInput{
		TextMetadata:   textMetadata(input, ""),
		Handle:         *handle,
		Delta:          delta.Text,
		ToolStatus:     delta.ToolStatus,
		ThoughtSummary: delta.ThoughtSummary,
	}); err != nil {
		workflow.GetLogger(ctx).Warn("streamResp: stream update failed", "error", err)
	}
}

// endStream closes handle's stream. fullText/citations/segments are the segment's
// complete content, passed through so an outbound driver that can only append while
// streaming can correct the finished message afterward (see
// slackoutbound.SlackPlatform.FinishStream).
func (w *RouterWorkflow) endStream(ctx workflow.Context, input Input, handle *StreamHandle, fullText string, citations []Citation, segments []Delta) {
	if handle == nil {
		return
	}
	metadata := textMetadata(input, fullText)
	metadata.Citations = citations
	metadata.Segments = segments
	if err := w.outbound.FinishStream(ctx, FinishStreamInput{
		TextMetadata: metadata,
		Handle:       *handle,
	}); err != nil {
		workflow.GetLogger(ctx).Warn("streamResp: stream finish failed", "error", err)
	}
}
