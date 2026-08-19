// Package teamsoutbound implements the workflow-side Microsoft Teams outbound driver.
// The actual Bot Framework I/O runs in the Python Teams activity worker because
// Microsoft does not provide a Teams SDK for Go.
package teamsoutbound

import (
	"fmt"
	"strings"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/citations"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"go.temporal.io/sdk/workflow"
)

const (
	beginStreamActivity        = "BeginStream"
	updateStreamActivity       = "UpdateStream"
	finishStreamActivity       = "FinishStream"
	postMessageActivity        = "PostMessage"
	postApprovalPromptActivity = "PostApprovalPrompt"
	updateMessageActivity      = "UpdateActivity" // Keep the registered activity name stable for compatibility.
)

const pinnedActivityScheduleToStartTimeout = 10 * time.Second

// Driver dispatches durable outbound operations to the Python Teams activity
// worker, pinning each stream to the worker process that opened it.
type Driver struct {
	ActivityOptions workflow.ActivityOptions
}

var _ router.OutboundDriver = (*Driver)(nil)

func NewDriver(opts workflow.ActivityOptions) Driver {
	return Driver{ActivityOptions: opts}
}

func (d Driver) activityContext(ctx workflow.Context) workflow.Context {
	return workflow.WithActivityOptions(ctx, d.ActivityOptions)
}

// SupportsStreaming reports whether the Teams conversation can receive
// incremental response updates. Shared conversations require a complete
// response because Teams does not support native streaming there.
func (Driver) SupportsStreaming(input router.Input) bool {
	if input.Message == nil {
		return true
	}

	switch strings.ToLower(strings.TrimSpace(input.Message.ConversationType)) {
	case "channel", "groupchat":
		return false
	default:
		return true
	}
}

func (d Driver) streamActivityContext(ctx workflow.Context, handle router.StreamHandle) workflow.Context {
	if handle.TaskQueue == "" {
		return d.activityContext(ctx)
	}
	options := d.ActivityOptions
	options.TaskQueue = handle.TaskQueue
	options.ScheduleToStartTimeout = pinnedActivityScheduleToStartTimeout
	return workflow.WithActivityOptions(ctx, options)
}

func (d Driver) BeginStream(ctx workflow.Context, input router.BeginStreamInput) (router.StreamHandle, error) {
	var handle router.StreamHandle
	err := workflow.ExecuteActivity(d.activityContext(ctx), beginStreamActivity, input).Get(ctx, &handle)
	return handle, err
}

// UpdateStream flattens the delta's structured content (Text/ToolStatus/ThoughtSummary)
// into the plain text string the Python activity worker's contract expects, since Teams
// has no richer way to show tool status than the same inline markers it always used -
// see flattenDeltaText.
// UpdateStream skips scheduling the (pinned) activity entirely when the delta has no
// rendered text (e.g. a subagent or model-usage event Teams has no representation for),
// avoiding a wasted round trip to the pinned Python worker for events Teams can't render.
func (d Driver) UpdateStream(ctx workflow.Context, input router.UpdateStreamInput) error {
	input.Delta = flattenDeltaText(router.Delta{
		Text:           input.Delta,
		ToolStatus:     input.ToolStatus,
		ThoughtSummary: input.ThoughtSummary,
	})
	if input.Delta == "" {
		return nil
	}
	return workflow.ExecuteActivity(d.streamActivityContext(ctx, input.Handle), updateStreamActivity, input).Get(ctx, nil)
}

// TODO(long-nt-tran): citations aren't spliced into native-streaming Teams messages -
// the Python worker's finish_stream just closes the stream, with no way to correct it.
func (d Driver) FinishStream(ctx workflow.Context, input router.FinishStreamInput) error {
	return workflow.ExecuteActivity(d.streamActivityContext(ctx, input.Handle), finishStreamActivity, input).Get(ctx, nil)
}

// PostMessage flattens every delta of the turn (input.Segments) into one plain text
// string (see flattenSegments), then splices in citation markers. Citations' EndIndex
// is an offset into the reply text alone, so it's retargeted to flattenSegments' output
// first - see retargetCitations.
func (d Driver) PostMessage(ctx workflow.Context, input router.TextMetadata) error {
	text := flattenSegments(input.Segments)
	input.Text = citations.Splice(text, retargetCitations(input.Segments, input.Citations), citations.CommonMarkLink)
	return workflow.ExecuteActivity(d.activityContext(ctx), postMessageActivity, input).Get(ctx, nil)
}

func (d Driver) PostApprovalPrompt(ctx workflow.Context, input router.ApprovalPromptInput) error {
	return workflow.ExecuteActivity(d.activityContext(ctx), postApprovalPromptActivity, input).Get(ctx, nil)
}

// flattenDeltaText reproduces the plain-text formatting the connector's agent driver
// used to bake directly into Delta.Text before ToolStatus/ThoughtSummary became separate
// structured fields (see agent/driver.go's turnEventToDelta). Teams has no richer way to
// show tool status than inline text, so its driver rebuilds the exact same markers
// itself instead of receiving them pre-flattened.
func flattenDeltaText(d router.Delta) string {
	switch {
	case d.ToolStatus != nil:
		switch d.ToolStatus.Status {
		case router.ToolStarted:
			return "\n_" + d.ToolStatus.ToolName + "..._"
		case router.ToolCompleted:
			return " ✅\n\n"
		case router.ToolErrored:
			return " ❌ Error: " + d.ToolStatus.Message + "\n\n"
		default:
			return ""
		}
	case d.ThoughtSummary != "":
		return d.ThoughtSummary
	default:
		return d.Text
	}
}

// flattenSegments concatenates every delta's flattened text in order, for the
// non-streaming PostMessage path where a whole turn arrives as one call.
func flattenSegments(segments []router.Delta) string {
	var b strings.Builder
	for _, d := range segments {
		b.WriteString(flattenDeltaText(d))
	}
	return b.String()
}

// retargetCitations rewrites each citation's EndIndex from an offset into the reply
// text alone to the matching offset in flattenSegments' output, which interleaves tool
// status/thought summary text between reply chunks.
func retargetCitations(segments []router.Delta, cs []router.Citation) []router.Citation {
	if len(cs) == 0 {
		return cs
	}
	retargeted := make([]router.Citation, len(cs))
	for i, c := range cs {
		c.EndIndex = retargetEndIndex(segments, c.EndIndex)
		retargeted[i] = c
	}
	return retargeted
}

func retargetEndIndex(segments []router.Delta, endIndex int) int {
	if endIndex < 0 {
		return -1
	}
	var replyOffset, flatOffset int
	for _, seg := range segments {
		if seg.Text != "" {
			replyLen := len([]rune(seg.Text))
			if endIndex <= replyOffset+replyLen {
				return flatOffset + (endIndex - replyOffset)
			}
			replyOffset += replyLen
		}
		flatOffset += len([]rune(flattenDeltaText(seg)))
	}
	return flatOffset
}

func (d Driver) AcknowledgeApproval(ctx workflow.Context, input router.ApprovalAcknowledgementInput) error {
	if input.PromptID == "" {
		return nil
	}
	decision := "❌ Denied"
	if input.Approved {
		decision = "✅ Approved"
	}
	input.Text = fmt.Sprintf("🔐 Tool `%s`: %s", input.ToolName, decision)
	return workflow.ExecuteActivity(d.activityContext(ctx), updateMessageActivity, router.UpdateMessageInput{
		TextMetadata: input.TextMetadata,
		MessageID:    input.PromptID,
	}).Get(ctx, nil)
}
