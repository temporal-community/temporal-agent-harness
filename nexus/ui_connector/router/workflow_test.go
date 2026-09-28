package router

import (
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"

	"github.com/temporal-community/temporal-agent-harness/nexus/ui_connector/protocol"
)

type fakeBackend struct {
	start    Start
	startErr error
	polls    []Poll
	pollErr  error
	handles  []string
}

func (f *fakeBackend) Start(workflow.Context, protocol.Request) (Start, error) {
	return f.start, f.startErr
}

func (f *fakeBackend) Poll(_ workflow.Context, h Handle) (Poll, error) {
	f.handles = append(f.handles, string(h))
	if len(f.polls) == 0 {
		return Poll{}, f.pollErr
	}
	p := f.polls[0]
	f.polls = f.polls[1:]
	return p, nil
}

// fakeOutbound records each delivery. Its state counts deliveries.
type fakeOutbound struct {
	batches  [][]protocol.Event
	states   []string
	done     []string
	fail     bool
	interval time.Duration
}

func (f *fakeOutbound) Deliver(_ workflow.Context, _ Address, state State, events []protocol.Event) (State, error) {
	f.batches = append(f.batches, events)
	f.states = append(f.states, string(state))
	if f.fail {
		return nil, errors.New("down")
	}
	return json.RawMessage(`"` + string(rune('0'+len(f.batches))) + `"`), nil
}

func (f *fakeOutbound) Done(_ workflow.Context, _ Address, state State) error {
	f.done = append(f.done, string(state))
	return nil
}

type pacedOutbound struct{ *fakeOutbound }

func (p pacedOutbound) PollInterval() time.Duration { return p.interval }

func text(s string) protocol.Event { return protocol.Event{Chunk: &protocol.Chunk{Text: s}} }

func run(t *testing.T, out Outbound, backend Backend) *testsuite.TestWorkflowEnvironment {
	t.Helper()
	w := NewRouterWorkflow(out, backend)
	env := (&testsuite.WorkflowTestSuite{}).NewTestWorkflowEnvironment()
	env.RegisterWorkflow(w.Run)
	env.ExecuteWorkflow(w.Run, Input{Request: protocol.Request{SessionID: "s"}, Address: json.RawMessage(`{}`)})
	require.True(t, env.IsWorkflowCompleted())
	require.NoError(t, env.GetWorkflowError())
	return env
}

func TestRun_DeliversEachBatchAndPassesState(t *testing.T) {
	out := &fakeOutbound{}
	backend := &fakeBackend{
		start: Start{Events: []protocol.Event{{TurnStarted: true}}, Handle: json.RawMessage(`"h0"`)},
		polls: []Poll{
			{Events: []protocol.Event{text("a")}, Handle: json.RawMessage(`"h1"`)},
			{}, // no events: no delivery
			{Events: []protocol.Event{text("b")}, Done: true},
		},
	}
	run(t, out, backend)

	assert.Len(t, out.batches, 3)
	assert.Equal(t, []string{"", `"1"`, `"2"`}, out.states, "each delivery gets the state the last one returned")
	assert.Equal(t, []string{`"3"`}, out.done)
	assert.Equal(t, []string{`"h0"`, `"h1"`, `"h1"`}, backend.handles, "a poll without a handle keeps the last one")
}

func TestRun_NoHandleMeansNoPoll(t *testing.T) {
	out := &fakeOutbound{}
	backend := &fakeBackend{start: Start{Events: []protocol.Event{text("reply")}}}
	run(t, out, backend)
	assert.Len(t, out.batches, 1)
	assert.Empty(t, backend.handles)
	assert.Equal(t, []string{`"1"`}, out.done)
}

func TestRun_StartErrorIsDeliveredAsAnEvent(t *testing.T) {
	out := &fakeOutbound{}
	run(t, out, &fakeBackend{startErr: errors.New("nexus down")})
	require.Len(t, out.batches, 1)
	assert.Contains(t, out.batches[0][0].Error, "nexus down")
}

func TestEmpty(t *testing.T) {
	assert.True(t, Empty(nil))
	assert.True(t, Empty(json.RawMessage("null")))
	assert.False(t, Empty(json.RawMessage(`{}`)))
}

func TestRun_PollErrorEndsTheRun(t *testing.T) {
	out := &fakeOutbound{}
	run(t, out, &fakeBackend{start: Start{Handle: json.RawMessage(`"h"`)}, pollErr: errors.New("boom")})
	assert.Empty(t, out.batches)
	assert.Equal(t, []string{""}, out.done, "Done always runs")
}

func TestRun_FailedDeliveryKeepsTheLastState(t *testing.T) {
	out := &fakeOutbound{fail: true}
	run(t, out, &fakeBackend{start: Start{Events: []protocol.Event{text("a")}}})
	assert.Equal(t, []string{""}, out.done)
}

func TestRun_PacerWaitsBetweenPolls(t *testing.T) {
	out := &fakeOutbound{interval: time.Minute}
	backend := &fakeBackend{
		start: Start{Handle: json.RawMessage(`"h"`)},
		polls: []Poll{{Handle: json.RawMessage(`"h"`)}, {Done: true}},
	}
	start := time.Now()
	env := run(t, pacedOutbound{out}, backend)
	assert.GreaterOrEqual(t, env.Now().Sub(start), time.Minute, "one wait before the second poll")
	assert.Len(t, backend.handles, 2)
}
