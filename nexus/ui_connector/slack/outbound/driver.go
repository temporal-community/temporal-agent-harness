// Package slackoutbound shows agent output in Slack.
package slackoutbound

import (
	"encoding/json"
	"time"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

const deliverActivity = "SlackDeliver"

// Driver implements router.Outbound with one Slack activity per delivery.
type Driver struct {
	ActivityOptions workflow.ActivityOptions
	// Interval sets the wait between polls. Zero means no wait.
	Interval time.Duration
}

var (
	_ router.Outbound = Driver{}
	_ router.Pacer    = Driver{}
)

func NewDriver(opts workflow.ActivityOptions) Driver {
	return Driver{ActivityOptions: opts}
}

// DeliverInput is the SlackDeliver activity input.
type DeliverInput struct {
	Address json.RawMessage
	State   json.RawMessage
	Events  []protocol.Event
	// End closes the open message and clears the status.
	End bool
}

func (d Driver) Deliver(ctx workflow.Context, address router.Address, state router.State, events []protocol.Event) (router.State, error) {
	return d.run(ctx, DeliverInput{Address: address, State: state, Events: events})
}

func (d Driver) Done(ctx workflow.Context, address router.Address, state router.State) error {
	if router.Empty(state) {
		return nil
	}
	_, err := d.run(ctx, DeliverInput{Address: address, State: state, End: true})
	return err
}

func (d Driver) PollInterval() time.Duration { return d.Interval }

func (d Driver) run(ctx workflow.Context, in DeliverInput) (router.State, error) {
	var out router.State
	err := workflow.ExecuteActivity(workflow.WithActivityOptions(ctx, d.ActivityOptions), deliverActivity, in).Get(ctx, &out)
	return out, err
}

// RegisterActivities registers the Slack activity used by Driver.
func RegisterActivities(w worker.Registry, platform *SlackPlatform) {
	w.RegisterActivityWithOptions(platform.Deliver, activity.RegisterOptions{Name: deliverActivity})
}
