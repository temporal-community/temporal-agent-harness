// Package teams implements the workflow-side Microsoft Teams inbound driver.
// The actual Bot Framework I/O runs in the Python Teams activity worker.
package teams

import (
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/inbound"
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

// Driver dispatches durable inbound operations to the Python Teams activity
// worker polling the same task queue.
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

func (d Driver) BeginStream(ctx workflow.Context, input inbound.BeginStreamInput) (inbound.StreamHandle, error) {
	var handle inbound.StreamHandle
	err := workflow.ExecuteActivity(d.activityContext(ctx), beginStreamActivity, input).Get(ctx, &handle)
	return handle, err
}

func (d Driver) UpdateStream(ctx workflow.Context, input inbound.UpdateStreamInput) error {
	return workflow.ExecuteActivity(d.activityContext(ctx), updateStreamActivity, input).Get(ctx, nil)
}

func (d Driver) FinishStream(ctx workflow.Context, input inbound.FinishStreamInput) error {
	return workflow.ExecuteActivity(d.activityContext(ctx), finishStreamActivity, input).Get(ctx, nil)
}

func (d Driver) PostMessage(ctx workflow.Context, input inbound.TextMetadata) error {
	return workflow.ExecuteActivity(d.activityContext(ctx), postMessageActivity, input).Get(ctx, nil)
}

func (d Driver) PostApprovalPrompt(ctx workflow.Context, input inbound.ApprovalPromptInput) error {
	return workflow.ExecuteActivity(d.activityContext(ctx), postApprovalPromptActivity, input).Get(ctx, nil)
}

func (d Driver) UpdateMessage(ctx workflow.Context, input inbound.UpdateMessageInput) error {
	return workflow.ExecuteActivity(d.activityContext(ctx), updateMessageActivity, input).Get(ctx, nil)
}
