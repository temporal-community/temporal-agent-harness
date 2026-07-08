//go:build integration

// Integration durability tests for the nexus-agent Go Nexus handler.
//
// These tests verify that the handler survives worker restarts, that multiple
// workers share a task queue without interference, and that pollMessages returns
// Closed=true when the agent workflow has already completed.
//
// Note: the Go SDK reserves update names starting with "__" for internal use,
// so __temporal_workflow_stream_poll (the Python WorkflowStream update) cannot
// be registered in Go, hence we need to mock out the nexus-agent/ entirely here.
package nexusinteg

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	h "github.com/temporalio/temporal-agent-harness/nexus/agent_adapter/nexus_worker/handler"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/temporal"
	sdkworker "go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

const nexusAgentEndpoint = "test-nexus-agent-endpoint"

// mockAgentWorkflowName/mockAgentWorkflowIDPrefix stand in for the values a real
// deployment would set via handler.Config — the handler used to hardcode a single
// agent's workflow name/ID prefix; now they're caller-supplied, so the test supplies
// its own.
const mockAgentWorkflowName = "QaAgent"
const mockAgentWorkflowIDPrefix = "qa-agent-"

// -- Tests --------------------------------------------------------------------

func TestAgent_SendMessage_HandlerWorkerRestart(t *testing.T) {
	// Handler worker W0 processes turn 1; W1 handles turn 2 on the same session
	// after W0 is stopped — verifies the handler is stateless.
	devserver := NewDevServer(t)
	temporalClient := devserver.Client()

	handlerTaskQueue := TaskQueue(t, "integ-handler-")
	agentTaskQueue := TaskQueue(t, "integ-agent-")
	callerTaskQueue := TaskQueue(t, "integ-caller-")

	CreateNexusEndpoint(t, temporalClient, nexusAgentEndpoint, handlerTaskQueue)
	startFakeAgentWorkerInteg(t, temporalClient, agentTaskQueue)
	startCallerWorkerInteg(t, temporalClient, callerTaskQueue)

	handlerWorker0 := startNexusHandlerWorker(t, temporalClient, handlerTaskQueue, agentTaskQueue)

	run1, err := temporalClient.ExecuteWorkflow(context.Background(), client.StartWorkflowOptions{
		ID: "caller-restart-msg1", TaskQueue: callerTaskQueue,
	}, callerWorkflow, callerInput{SessionID: "restart-sess", Message: "hello"})
	require.NoError(t, err)
	var out1 callerOutput
	require.NoError(t, run1.Get(context.Background(), &out1))
	assert.Equal(t, int64(1), out1.TurnNumber)

	handlerWorker0.Stop()

	handlerWorker1 := startNexusHandlerWorker(t, temporalClient, handlerTaskQueue, agentTaskQueue)
	defer handlerWorker1.Stop()

	run2, err := temporalClient.ExecuteWorkflow(context.Background(), client.StartWorkflowOptions{
		ID: "caller-restart-msg2", TaskQueue: callerTaskQueue,
	}, callerWorkflow, callerInput{SessionID: "restart-sess", Message: "world"})
	require.NoError(t, err)
	var out2 callerOutput
	require.NoError(t, run2.Get(context.Background(), &out2))
	assert.Equal(t, int64(2), out2.TurnNumber,
		"turn counter must advance after handler restart — state lives in agent workflow, not handler worker")
}

func TestAgent_MultipleHandlerWorkers(t *testing.T) {
	// Two handler workers share the same task queue; after handlerWorker0 is stopped,
	// all remaining requests are handled by handlerWorker1.
	devserver := NewDevServer(t)
	temporalClient := devserver.Client()

	handlerTaskQueue := TaskQueue(t, "integ-handler-")
	agentTaskQueue := TaskQueue(t, "integ-agent-")
	callerTaskQueue := TaskQueue(t, "integ-caller-")

	CreateNexusEndpoint(t, temporalClient, nexusAgentEndpoint, handlerTaskQueue)
	startFakeAgentWorkerInteg(t, temporalClient, agentTaskQueue)
	startCallerWorkerInteg(t, temporalClient, callerTaskQueue)

	handlerWorker0 := startNexusHandlerWorker(t, temporalClient, handlerTaskQueue, agentTaskQueue)
	handlerWorker1 := startNexusHandlerWorker(t, temporalClient, handlerTaskQueue, agentTaskQueue)
	defer handlerWorker1.Stop()

	sessions := []string{"multi-a", "multi-b", "multi-c"}

	for _, sess := range sessions {
		_, err := temporalClient.ExecuteWorkflow(context.Background(), client.StartWorkflowOptions{
			ID: "caller-" + sess + "-r1", TaskQueue: callerTaskQueue,
		}, callerWorkflow, callerInput{SessionID: sess, Message: "ping"})
		require.NoError(t, err, "session %s", sess)
	}
	for _, sess := range sessions {
		var out callerOutput
		require.NoError(t, temporalClient.GetWorkflow(context.Background(), "caller-"+sess+"-r1", "").
			Get(context.Background(), &out), "session %s", sess)
		assert.Equal(t, int64(1), out.TurnNumber, "session %s round 1", sess)
	}

	handlerWorker0.Stop()

	for _, sess := range sessions {
		_, err := temporalClient.ExecuteWorkflow(context.Background(), client.StartWorkflowOptions{
			ID: "caller-" + sess + "-r2", TaskQueue: callerTaskQueue,
		}, callerWorkflow, callerInput{SessionID: sess, Message: "pong"})
		require.NoError(t, err, "session %s", sess)
	}
	for _, sess := range sessions {
		var out callerOutput
		require.NoError(t, temporalClient.GetWorkflow(context.Background(), "caller-"+sess+"-r2", "").
			Get(context.Background(), &out), "session %s", sess)
		assert.Equal(t, int64(2), out.TurnNumber, "session %s round 2 must be turn 2", sess)
	}
}

func TestAgent_PollMessages_WorkflowCompleted(t *testing.T) {
	// When QaAgentWorkflow has completed, pollMessages must return Closed=true.
	devserver := NewDevServer(t)
	temporalClient := devserver.Client()

	handlerTaskQueue := TaskQueue(t, "integ-handler-")
	agentTaskQueue := TaskQueue(t, "integ-agent-")
	callerTaskQueue := TaskQueue(t, "integ-caller-")

	CreateNexusEndpoint(t, temporalClient, nexusAgentEndpoint, handlerTaskQueue)
	startFakeAgentWorkerInteg(t, temporalClient, agentTaskQueue)
	startCallerWorkerInteg(t, temporalClient, callerTaskQueue)
	handlerWorker := startNexusHandlerWorker(t, temporalClient, handlerTaskQueue, agentTaskQueue)
	defer handlerWorker.Stop()

	const sessionID = "closed-sess"
	agentWfID := mockAgentWorkflowIDPrefix + sessionID

	run, err := temporalClient.ExecuteWorkflow(context.Background(), client.StartWorkflowOptions{
		ID: "caller-closed-msg1", TaskQueue: callerTaskQueue,
	}, callerWorkflow, callerInput{SessionID: sessionID, Message: "hello"})
	require.NoError(t, err)
	var out callerOutput
	require.NoError(t, run.Get(context.Background(), &out))
	assert.Equal(t, int64(1), out.TurnNumber)

	require.NoError(t, temporalClient.SignalWorkflow(context.Background(), agentWfID, "", "fake-shutdown", nil))

	require.Eventually(t, func() bool {
		desc, err := temporalClient.DescribeWorkflowExecution(context.Background(), agentWfID, "")
		return err == nil && desc.WorkflowExecutionInfo.CloseTime != nil
	}, 15*time.Second, 200*time.Millisecond, "agent workflow should have closed after fake-shutdown signal")

	pollRun, err := temporalClient.ExecuteWorkflow(context.Background(), client.StartWorkflowOptions{
		ID: "caller-closed-poll", TaskQueue: callerTaskQueue,
	}, pollOnlyWorkflow, sessionID)
	require.NoError(t, err)
	var closed bool
	require.NoError(t, pollRun.Get(context.Background(), &closed))
	assert.True(t, closed, "pollMessages must return Closed=true when the agent workflow has completed")
}

// -- Infrastructure helpers ---------------------------------------------------

func startNexusHandlerWorker(t *testing.T, temporalClient client.Client, handlerTaskQueue, agentTaskQueue string) sdkworker.Worker {
	t.Helper()
	w := sdkworker.New(temporalClient, handlerTaskQueue, sdkworker.Options{DisableWorkflowWorker: true})
	w.RegisterNexusService(h.NewAgentNexusService(h.Config{
		AgentTaskQueue:          agentTaskQueue,
		WorkflowName:            mockAgentWorkflowName,
		WorkflowIDPrefix:        mockAgentWorkflowIDPrefix,
		IsMessageQueuingEnabled: true,
	}))
	require.NoError(t, w.Start())
	return w
}

func startFakeAgentWorkerInteg(t *testing.T, temporalClient client.Client, agentTaskQueue string) sdkworker.Worker {
	t.Helper()
	w := sdkworker.New(temporalClient, agentTaskQueue, sdkworker.Options{})
	w.RegisterWorkflowWithOptions(mockQaAgentWorkflow, workflow.RegisterOptions{Name: mockAgentWorkflowName})
	require.NoError(t, w.Start())
	t.Cleanup(func() { w.Stop() })
	return w
}

func startCallerWorkerInteg(t *testing.T, temporalClient client.Client, callerTQ string) sdkworker.Worker {
	t.Helper()
	w := sdkworker.New(temporalClient, callerTQ, sdkworker.Options{})
	w.RegisterWorkflow(callerWorkflow)
	w.RegisterWorkflow(pollOnlyWorkflow)
	require.NoError(t, w.Start())
	t.Cleanup(func() { w.Stop() })
	return w
}

// -- mockQaAgentWorkflow ------------------------------------------------------

// mockAgentStartConfig mirrors the JSON shape the handler sends when starting the
// agent workflow (handler.agentStartConfig is unexported, so we replicate its wire
// shape here rather than depend on package internals).
type mockAgentStartConfig struct {
	IsMessageQueuingEnabled bool `json:"is_message_queuing_enabled"`
}

// mockQaAgentWorkflow mimics QaAgentWorkflow in Go so the handler can be tested
// without a Python runtime. It handles send_agent_message updates and agent_status
// queries, then blocks until the "fake-shutdown" signal arrives.
func mockQaAgentWorkflow(ctx workflow.Context, cfg mockAgentStartConfig) error {
	var currentTurn int

	if err := workflow.SetQueryHandler(ctx, h.AgentStatusQuery, func() (h.AgentStatus, error) {
		return h.AgentStatus{CurrentTurn: currentTurn}, nil
	}); err != nil {
		return err
	}

	if err := workflow.SetUpdateHandlerWithOptions(ctx, h.SendAgentMessageUpdate,
		func(ctx workflow.Context, input h.AgentMessage) (h.UserInputResult, error) {
			currentTurn++
			return h.UserInputResult{
				TurnNumber: currentTurn,
				TurnID:     fmt.Sprintf("turn-%d", currentTurn),
			}, nil
		},
		workflow.UpdateHandlerOptions{
			Validator: func(ctx workflow.Context, input h.AgentMessage) error {
				if input.ExpectedTurn != currentTurn+1 {
					return temporal.NewApplicationError(
						fmt.Sprintf("stale: expected turn %d, got %d", currentTurn+1, input.ExpectedTurn),
						"StaleTurn",
					)
				}
				return nil
			},
		},
	); err != nil {
		return err
	}

	// Block until the test sends "fake-shutdown".
	// Must use a non-nil pointer: Receive(ctx, nil) returns immediately in SDK v1.41.
	var done struct{}
	workflow.GetSignalChannel(ctx, "fake-shutdown").Receive(ctx, &done)
	return nil
}

// -- Caller workflows ---------------------------------------------------------

type callerInput struct {
	SessionID string `json:"session_id"`
	Message   string `json:"message"`
}

type callerOutput struct {
	TurnNumber int64 `json:"turn_number"`
}

func callerWorkflow(ctx workflow.Context, input callerInput) (callerOutput, error) {
	nc := workflow.NewNexusClient(nexusAgentEndpoint, h.AgentService.ServiceName)
	opOpts := workflow.NexusOperationOptions{ScheduleToCloseTimeout: 60 * time.Second}

	var sendOut h.SendMessageOutput
	if err := nc.ExecuteOperation(ctx, h.AgentService.SendAgentMessage, h.SendAgentMessageInput{
		SessionID: input.SessionID,
		MsgType:   "ask",
		Payload:   fmt.Sprintf(`{"text":%q}`, input.Message),
	}, opOpts).Get(ctx, &sendOut); err != nil {
		return callerOutput{}, fmt.Errorf("sendAgentMessage: %w", err)
	}
	return callerOutput{TurnNumber: sendOut.TurnNumber}, nil
}

func pollOnlyWorkflow(ctx workflow.Context, sessionID string) (bool, error) {
	nc := workflow.NewNexusClient(nexusAgentEndpoint, h.AgentService.ServiceName)
	var pollOut h.PollMessagesOutput
	if err := nc.ExecuteOperation(ctx, h.AgentService.PollMessages, h.PollMessagesInput{
		SessionID:      sessionID,
		Cursor:         0,
		TimeoutSeconds: 5,
	}, workflow.NexusOperationOptions{ScheduleToCloseTimeout: 30 * time.Second}).Get(ctx, &pollOut); err != nil {
		return false, err
	}
	return pollOut.Closed, nil
}
