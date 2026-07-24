// Package teams implements the workflow-side Microsoft Teams inbound driver.
// The actual Bot Framework I/O runs in the Python Teams activity worker because
// Microsoft does not provide a Teams SDK for Go.
package teams

import (
	"fmt"
	"strings"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/wire"
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

// Driver dispatches durable inbound operations to the Python Teams activity
// worker, pinning each stream to the worker process that opened it.
type Driver struct {
	ActivityOptions workflow.ActivityOptions
}

var _ inbound.Driver = (*Driver)(nil)

func NewDriver(opts workflow.ActivityOptions) Driver {
	return Driver{ActivityOptions: opts}
}

func (d Driver) activityContext(ctx workflow.Context) workflow.Context {
	return workflow.WithActivityOptions(ctx, d.ActivityOptions)
}

// SupportsStreaming reports whether the Teams conversation can receive
// incremental response updates. Shared conversations require a complete
// response because Teams does not support native streaming there.
func (Driver) SupportsStreaming(input wire.Input) bool {
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

func (d Driver) streamActivityContext(ctx workflow.Context, handle inbound.StreamHandle) workflow.Context {
	if handle.TaskQueue == "" {
		return d.activityContext(ctx)
	}
	options := d.ActivityOptions
	options.TaskQueue = handle.TaskQueue
	options.ScheduleToStartTimeout = pinnedActivityScheduleToStartTimeout
	return workflow.WithActivityOptions(ctx, options)
}

func (d Driver) BeginStream(ctx workflow.Context, input inbound.BeginStreamInput) (inbound.StreamHandle, error) {
	var handle inbound.StreamHandle
	err := workflow.ExecuteActivity(d.activityContext(ctx), beginStreamActivity, input).Get(ctx, &handle)
	return handle, err
}

func (d Driver) UpdateStream(ctx workflow.Context, input inbound.UpdateStreamInput) error {
	return workflow.ExecuteActivity(d.streamActivityContext(ctx, input.Handle), updateStreamActivity, input).Get(ctx, nil)
}

func (d Driver) FinishStream(ctx workflow.Context, input inbound.FinishStreamInput) error {
	return workflow.ExecuteActivity(d.streamActivityContext(ctx, input.Handle), finishStreamActivity, input).Get(ctx, nil)
}

func (d Driver) PostMessage(ctx workflow.Context, input inbound.TextMetadata) error {
	return workflow.ExecuteActivity(d.activityContext(ctx), postMessageActivity, input).Get(ctx, nil)
}

func (d Driver) PostApprovalPrompt(ctx workflow.Context, input inbound.ApprovalPromptInput) error {
	return workflow.ExecuteActivity(d.activityContext(ctx), postApprovalPromptActivity, input).Get(ctx, nil)
}

func (d Driver) AcknowledgeApproval(ctx workflow.Context, input inbound.ApprovalAcknowledgementInput) error {
	if input.PromptID == "" {
		return nil
	}
	decision := "❌ Denied"
	if input.Approved {
		decision = "✅ Approved"
	}
	input.Text = fmt.Sprintf("🔐 Tool `%s`: %s", input.ToolName, decision)
	return workflow.ExecuteActivity(d.activityContext(ctx), updateMessageActivity, inbound.UpdateMessageInput{
		TextMetadata: input.TextMetadata,
		MessageID:    input.PromptID,
	}).Get(ctx, nil)
}
