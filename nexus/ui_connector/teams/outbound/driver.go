// Package teamsoutbound implements the workflow-side Microsoft Teams outbound driver.
// The actual Bot Framework I/O runs in the Python Teams activity worker because
// Microsoft does not provide a Teams SDK for Go.
package teamsoutbound

import (
	"fmt"
	"strings"
	"time"

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

func (d Driver) UpdateStream(ctx workflow.Context, input router.UpdateStreamInput) error {
	return workflow.ExecuteActivity(d.streamActivityContext(ctx, input.Handle), updateStreamActivity, input).Get(ctx, nil)
}

func (d Driver) FinishStream(ctx workflow.Context, input router.FinishStreamInput) error {
	return workflow.ExecuteActivity(d.streamActivityContext(ctx, input.Handle), finishStreamActivity, input).Get(ctx, nil)
}

func (d Driver) PostMessage(ctx workflow.Context, input router.TextMetadata) error {
	return workflow.ExecuteActivity(d.activityContext(ctx), postMessageActivity, input).Get(ctx, nil)
}

func (d Driver) PostApprovalPrompt(ctx workflow.Context, input router.ApprovalPromptInput) error {
	return workflow.ExecuteActivity(d.activityContext(ctx), postApprovalPromptActivity, input).Get(ctx, nil)
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
