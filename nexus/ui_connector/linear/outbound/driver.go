package linearoutbound

import (
	"encoding/json"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/router"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

const deliverActivity = "LinearDeliver"

type Driver struct {
	ActivityOptions workflow.ActivityOptions
}

var _ router.Outbound = Driver{}

func NewDriver(opts workflow.ActivityOptions) Driver {
	return Driver{ActivityOptions: opts}
}

type DeliverInput struct {
	Address json.RawMessage
	State   json.RawMessage
	Events  []protocol.Event
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

func (d Driver) run(ctx workflow.Context, in DeliverInput) (router.State, error) {
	var out router.State
	err := workflow.ExecuteActivity(workflow.WithActivityOptions(ctx, d.ActivityOptions), deliverActivity, in).Get(ctx, &out)
	return out, err
}

func RegisterActivities(w worker.Registry, platform *LinearPlatform) {
	w.RegisterActivityWithOptions(platform.Deliver, activity.RegisterOptions{Name: deliverActivity})
}
