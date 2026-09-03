package router

import (
	"context"
	"errors"
	"time"

	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/temporal"
)

// Actions performs one-shot agent operations directly from a driver. The tunnel
// owns only stream polling and multicast; it is never an action proxy.
type Actions interface {
	SendMessage(context.Context, string, string, SendMessageInput) (TurnAccepted, error)
	Control(context.Context, string, string, ControlInput) (ControlOutput, error)
	Exists(context.Context, string) bool
}

// Client is the platform-facing connector port used by inbound drivers.
type Client interface {
	SendAndMount(context.Context, string, string, SendAndMountInput) (TurnAccepted, error)
	Control(context.Context, string, string, ControlInput) (ControlOutput, error)
	Exists(context.Context, string) bool
}

type TemporalClient struct {
	Client          client.Client
	TunnelTaskQueue string
	NexusEndpoint   string
	Actions         Actions
}

func NewClient(tc client.Client, taskQueue, nexusEndpoint string, actions Actions) *TemporalClient {
	return &TemporalClient{
		Client:          tc,
		TunnelTaskQueue: taskQueue,
		NexusEndpoint:   nexusEndpoint,
		Actions:         actions,
	}
}

// SendAndMount starts one standalone Nexus operation, then joins the deterministic
// tunnel for the accepted turn. The agent's retained A2A stream makes the boundary
// crash-safe: retrying the same operation ID returns the same action before mounting.
func (c *TemporalClient) SendAndMount(ctx context.Context, sessionID, operationID string, input SendAndMountInput) (TurnAccepted, error) {
	if c.Actions == nil {
		return TurnAccepted{}, errors.New("agent actions are required")
	}
	input.Message.SubscriberID = input.Subscriber.ID
	accepted, err := c.Actions.SendMessage(ctx, sessionID, operationID, input.Message)
	if err != nil || accepted.Reply != "" {
		return accepted, err
	}
	input.Subscriber.Cursor = accepted.StreamHeadOffset
	if err := c.Mount(ctx, sessionID, accepted.TurnNumber, accepted.StreamHeadOffset, input.Subscriber); err != nil {
		return TurnAccepted{}, err
	}
	return accepted, nil
}

// Mount registers one subscriber on the shared workflow for exactly one agent turn.
func (c *TemporalClient) Mount(ctx context.Context, sessionID string, turnNumber, fromOffset int64, subscriber Subscriber) error {
	if turnNumber <= 0 {
		return errors.New("turn number must be positive")
	}
	subscriber.Cursor = fromOffset
	start := c.Client.NewWithStartWorkflowOperation(
		client.StartWorkflowOptions{
			ID:                       TunnelWorkflowID(sessionID, turnNumber),
			TaskQueue:                c.TunnelTaskQueue,
			WorkflowIDConflictPolicy: enumspb.WORKFLOW_ID_CONFLICT_POLICY_USE_EXISTING,
			WorkflowIDReusePolicy:    enumspb.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE,
		},
		TunnelWorkflowName,
		TunnelInput{
			SessionID:     sessionID,
			NexusEndpoint: c.NexusEndpoint,
			TurnNumber:    turnNumber,
			FromOffset:    fromOffset,
		},
	)
	handle, err := c.Client.UpdateWithStartWorkflow(ctx, client.UpdateWithStartWorkflowOptions{
		StartWorkflowOperation: start,
		UpdateOptions: client.UpdateWorkflowOptions{
			UpdateID:     "mount-" + subscriber.ID,
			UpdateName:   RegisterSubscriberUpdate,
			Args:         []interface{}{RegisterSubscriberInput{Subscriber: subscriber}},
			WaitForStage: client.WorkflowUpdateStageCompleted,
		},
	})
	if err != nil {
		return err
	}
	return handle.Get(ctx, nil)
}

func (c *TemporalClient) Control(ctx context.Context, sessionID, operationID string, input ControlInput) (ControlOutput, error) {
	if c.Actions == nil {
		return ControlOutput{}, errors.New("agent actions are required")
	}
	output, err := c.Actions.Control(ctx, sessionID, operationID, input)
	if err != nil || input.Delivery == nil {
		return output, err
	}
	target := input.Delivery
	handle, err := c.Client.ExecuteActivity(ctx, client.StartActivityOptions{
		ID:                  operationID + "-delivery",
		TaskQueue:           target.TaskQueue,
		StartToCloseTimeout: time.Minute,
		RetryPolicy:         &temporal.RetryPolicy{MaximumAttempts: 3},
	}, target.Activity, ControlDeliveryInput{Context: target.Context, Result: output})
	if err != nil {
		return ControlOutput{}, err
	}
	var ignored any
	if err := handle.Get(ctx, &ignored); err != nil {
		return ControlOutput{}, err
	}
	return output, nil
}

func (c *TemporalClient) Exists(ctx context.Context, sessionID string) bool {
	return c.Actions != nil && c.Actions.Exists(ctx, sessionID)
}
