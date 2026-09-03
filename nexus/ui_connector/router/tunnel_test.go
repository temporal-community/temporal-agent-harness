package router

import (
	"fmt"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
)

type tunnelBackend struct {
	pages     []StreamPage
	pollCalls int
	timeouts  []float64
}

func (b *tunnelBackend) Poll(_ workflow.Context, _ TunnelInput, _ int64, timeout float64) (StreamPage, error) {
	b.pollCalls++
	b.timeouts = append(b.timeouts, timeout)
	if len(b.pages) == 0 {
		return StreamPage{Closed: true}, nil
	}
	page := b.pages[0]
	b.pages = b.pages[1:]
	return page, nil
}

func update(env *testsuite.TestWorkflowEnvironment, name, id string, input any, complete func(any, error)) {
	env.UpdateWorkflow(name, id, &testsuite.TestUpdateCallback{
		OnReject:   func(err error) { complete(nil, err) },
		OnAccept:   func() {},
		OnComplete: complete,
	}, input)
}

func TestTunnelMulticastsUntouchedA2ARecordsWithIndependentCursors(t *testing.T) {
	raw := "opaque-serialized-a2a"
	backend := &tunnelBackend{pages: []StreamPage{{
		Items: []StreamItem{{Offset: 0, Data: raw}}, NextCursor: 1, TurnComplete: true,
	}}}
	tunnel := NewTunnelWorkflow(backend)
	var pages []ReadEventsOutput
	reads := 0

	suite := testsuite.WorkflowTestSuite{}
	env := suite.NewTestWorkflowEnvironment()
	env.RegisterWorkflow(tunnel.Run)
	env.RegisterDelayedCallback(func() {
		update(env, RegisterSubscriberUpdate, "register-browser",
			RegisterSubscriberInput{Subscriber: Subscriber{ID: "browser", Mode: Observer}},
			func(_ any, err error) {
				require.NoError(t, err)
				update(env, RegisterSubscriberUpdate, "register-slack",
					RegisterSubscriberInput{Subscriber: Subscriber{ID: "slack", Mode: Observer}},
					func(_ any, err error) {
						require.NoError(t, err)
						for _, id := range []string{"browser", "slack"} {
							id := id
							update(env, ReadEventsUpdate, "read-"+id,
								ReadEventsInput{SubscriberID: id, MaximumItems: 10, WaitSeconds: 1},
								func(value any, err error) {
									require.NoError(t, err)
									pages = append(pages, value.(ReadEventsOutput))
									reads++
								})
						}
					})
			})
	}, time.Millisecond)
	env.ExecuteWorkflow(tunnel.Run, TunnelInput{SessionID: "agent-1", NexusEndpoint: "endpoint", TurnNumber: 1})
	require.NoError(t, env.GetWorkflowError())

	require.Equal(t, 1, backend.pollCalls, "subscribers share one A2A poll")
	require.Len(t, pages, 2)
	for _, page := range pages {
		require.Equal(t, raw, page.Items[0].Data)
		require.EqualValues(t, 1, page.NextCursor)
	}
}

func TestTunnelReplaysOnlySubscriberThatFellBehind(t *testing.T) {
	initial := make([]StreamItem, maxBufferedEvents+1)
	for i := range initial {
		initial[i] = StreamItem{Offset: int64(i), Data: fmt.Sprintf("event-%d", i)}
	}
	backend := &tunnelBackend{pages: []StreamPage{
		{Items: initial, NextCursor: int64(len(initial)), TurnComplete: true},
		{Items: []StreamItem{{Offset: 0, Data: "replayed"}}, NextCursor: 1, TurnComplete: true},
	}}
	tunnel := NewTunnelWorkflow(backend)
	var page ReadEventsOutput

	suite := testsuite.WorkflowTestSuite{}
	env := suite.NewTestWorkflowEnvironment()
	env.RegisterWorkflow(tunnel.Run)
	env.RegisterDelayedCallback(func() {
		update(env, RegisterSubscriberUpdate, "register", RegisterSubscriberInput{
			Subscriber: Subscriber{ID: "slow", Mode: Observer},
		}, func(_ any, err error) {
			require.NoError(t, err)
			update(env, ReadEventsUpdate, "read", ReadEventsInput{SubscriberID: "slow", MaximumItems: 1},
				func(value any, err error) {
					require.NoError(t, err)
					page = value.(ReadEventsOutput)
				})
		})
	}, time.Millisecond)
	env.ExecuteWorkflow(tunnel.Run, TunnelInput{SessionID: "agent-1", TurnNumber: 1})
	require.NoError(t, env.GetWorkflowError())

	require.True(t, page.Replayed)
	require.Equal(t, "replayed", page.Items[0].Data)
	require.Equal(t, 2, backend.pollCalls)
}

func TestTunnelReceiverDoesNotLeakStateBetweenExecutions(t *testing.T) {
	backend := &tunnelBackend{}
	tunnel := NewTunnelWorkflow(backend)
	suite := testsuite.WorkflowTestSuite{}

	for _, sessionID := range []string{"agent-1", "agent-2"} {
		env := suite.NewTestWorkflowEnvironment()
		env.RegisterWorkflow(tunnel.Run)
		env.RegisterDelayedCallback(func() {
			update(env, RegisterSubscriberUpdate, "register-"+sessionID, RegisterSubscriberInput{
				Subscriber: Subscriber{ID: "browser-" + sessionID, Mode: Observer},
			}, func(_ any, err error) {
				require.NoError(t, err)
				env.SignalWorkflow(StopTunnelSignal, struct{}{})
			})
		}, time.Millisecond)
		env.ExecuteWorkflow(tunnel.Run, TunnelInput{SessionID: sessionID, TurnNumber: 1})
		require.NoError(t, env.GetWorkflowError())
	}

	// The registered receiver contains dependency injection only. Each invocation
	// owns a separate execution object and cannot retain another session's users.
	require.Nil(t, tunnel.subs)
	require.Nil(t, tunnel.subOrder)
}

func TestTunnelWorkflowIDIsScopedToOneTurn(t *testing.T) {
	require.Equal(t, "ui-tunnel-agent-1-turn-7", TunnelWorkflowID("agent-1", 7))
}

func TestKnownCompleteTurnDrainsWithoutLongPolling(t *testing.T) {
	backend := &tunnelBackend{pages: []StreamPage{{NextCursor: 12}}}
	tunnel := NewTunnelWorkflow(backend)
	suite := testsuite.WorkflowTestSuite{}
	env := suite.NewTestWorkflowEnvironment()
	env.RegisterWorkflow(tunnel.Run)
	env.RegisterDelayedCallback(func() {
		update(env, RegisterSubscriberUpdate, "register", RegisterSubscriberInput{
			Subscriber: Subscriber{ID: "browser", Mode: Observer, Cursor: 12},
		}, func(_ any, err error) {
			require.NoError(t, err)
			update(env, ReadEventsUpdate, "read", ReadEventsInput{
				SubscriberID: "browser", Cursor: 12,
			}, func(value any, err error) {
				require.NoError(t, err)
				require.True(t, value.(ReadEventsOutput).Closed)
			})
		})
	}, time.Millisecond)
	env.ExecuteWorkflow(tunnel.Run, TunnelInput{
		SessionID: "agent-1", TurnNumber: 1, FromOffset: 12, KnownComplete: true,
	})
	require.NoError(t, env.GetWorkflowError())
	require.Equal(t, []float64{.1}, backend.timeouts)
}
